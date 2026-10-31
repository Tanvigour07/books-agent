"""Tools the agent can call. Each tool returns a small JSON-serialisable dict
so the agent (LLM or rule-based) can reason about success/failure."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import urljoin

import pandas as pd
import requests
from bs4 import BeautifulSoup

# BOOKS_BASE_URL / BOOKS_DB_PATH are overridable for offline testing.
BASE = os.getenv("BOOKS_BASE_URL", "https://books.toscrape.com/catalogue/")
PAGE_URL = BASE + "page-{n}.html"
DB_PATH = Path(os.getenv("BOOKS_DB_PATH", "data/books.db"))
RATING_MAP = {"One": 1, "Two": 2, "Three": 3, "Four": 4, "Five": 5}

# Simulated-failure switch: the first fetch of these URLs raises, so the
# agent must demonstrate retry/recovery. Cleared after first trigger.
SIMULATE_FAILURE_ONCE: set[str] = set()
_triggered: set[str] = set()

_session = requests.Session()
_session.headers["User-Agent"] = "books-agent/1.0 (assignment)"


def _fetch(url: str, timeout: int = 15) -> str:
    if url in SIMULATE_FAILURE_ONCE and url not in _triggered:
        _triggered.add(url)
        raise requests.ConnectionError(f"SIMULATED failure fetching {url}")
    r = _session.get(url, timeout=timeout)
    r.raise_for_status()
    r.encoding = "utf-8"
    return r.text


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.execute("""CREATE TABLE IF NOT EXISTS bronze_books (
        product_url TEXT PRIMARY KEY, raw_json TEXT, scraped_at TEXT)""")
    return c


# ---------------------------------------------------------------- scraping
def scrape_listing_page(page: int) -> dict:
    """Fetch a catalogue page; return product URLs found (discovery)."""
    url = PAGE_URL.format(n=page)
    try:
        soup = BeautifulSoup(_fetch(url), "html.parser")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "page": page, "error": f"{type(e).__name__}: {e}"}
    urls = [urljoin(url, a["href"]) for a in soup.select("article.product_pod h3 a")]
    if not urls:
        return {"ok": False, "page": page, "error": "no products found on page"}
    return {"ok": True, "page": page, "product_urls": urls}


def scrape_product_page(url: str) -> dict:
    """Fetch one product page; return raw record (stored to Bronze)."""
    try:
        soup = BeautifulSoup(_fetch(url), "html.parser")
        main = soup.select_one("article.product_page")
        if main is None:
            return {"ok": False, "url": url, "error": "product_page markup missing"}
        crumbs = [a.get_text(strip=True) for a in soup.select("ul.breadcrumb li a")]
        rating_cls = main.select_one("p.star-rating")["class"]
        avail = main.select_one("p.availability").get_text(" ", strip=True)
        rec = {
            "title": main.select_one("h1").get_text(strip=True),
            "price_raw": main.select_one("p.price_color").get_text(strip=True),
            "rating_raw": next((c for c in rating_cls if c != "star-rating"), None),
            "category": crumbs[-1] if len(crumbs) >= 3 else None,
            "availability_raw": avail,
            "product_url": url,
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "url": url, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "url": url, "record": rec}


# ------------------------------------------------------------------ bronze
def save_bronze(records: list[dict]) -> dict:
    """Upsert raw records keyed by product_url (idempotent)."""
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    with _conn() as c:
        c.executemany(
            "INSERT INTO bronze_books VALUES (?,?,?) ON CONFLICT(product_url) "
            "DO UPDATE SET raw_json=excluded.raw_json, scraped_at=excluded.scraped_at",
            [(r["product_url"], json.dumps(r), now) for r in records],
        )
        n = c.execute("SELECT COUNT(*) FROM bronze_books").fetchone()[0]
    return {"ok": True, "bronze_rows": n}


# ---------------------------------------------------------------- validate
def validate_and_clean() -> dict:
    """Bronze -> Silver. Parse, validate, dedupe; report issues."""
    with _conn() as c:
        rows = [json.loads(r[0]) for r in c.execute("SELECT raw_json FROM bronze_books")]
    clean, issues = [], []
    for r in rows:
        url = r["product_url"]
        m = re.search(r"(\d+(?:\.\d+)?)", (r.get("price_raw") or "").replace(",", ""))
        price = float(m.group(1)) if m else None
        rating = RATING_MAP.get(r.get("rating_raw"))
        s = re.search(r"\((\d+)\s+available\)", r.get("availability_raw") or "")
        stock = int(s.group(1)) if s else None
        problem = None
        if not r.get("title"):
            problem = "missing title"
        elif price is None or price <= 0:
            problem = "missing/invalid price"
        elif rating is None:
            problem = "invalid rating"
        elif not r.get("category"):
            problem = "missing category"
        elif stock is None:
            problem = "missing stock"
        if problem:
            issues.append({"url": url, "problem": problem})
            continue
        clean.append(dict(title=r["title"], price_gbp=price, rating=rating,
                          category=r["category"], stock=stock, product_url=url))
    df = pd.DataFrame(clean)
    dups = int(df.duplicated(subset=["product_url"]).sum()) if len(df) else 0
    df = df.drop_duplicates(subset=["product_url"]) if len(df) else df
    with _conn() as c:
        c.execute("DROP TABLE IF EXISTS silver_books")
        if len(df):
            df.to_sql("silver_books", c, index=False)
        else:
            c.execute("CREATE TABLE silver_books (title, price_gbp, rating, category, stock, product_url)")
    return {"ok": True, "silver_rows": len(df), "invalid": issues,
            "duplicates_dropped": dups}


# -------------------------------------------------------------------- gold
def build_gold() -> dict:
    """Silver -> Gold aggregate tables used by the dashboard."""
    with _conn() as c:
        c.executescript("""
        DROP TABLE IF EXISTS gold_kpis;
        DROP TABLE IF EXISTS gold_by_category;
        DROP TABLE IF EXISTS gold_rating_dist;
        DROP TABLE IF EXISTS gold_top_expensive;
        CREATE TABLE gold_kpis AS SELECT COUNT(*) AS total_books,
            ROUND(AVG(price_gbp),2) AS avg_price, SUM(stock) AS total_stock
            FROM silver_books;
        CREATE TABLE gold_by_category AS SELECT category, COUNT(*) AS books,
            ROUND(AVG(price_gbp),2) AS avg_price FROM silver_books
            GROUP BY category ORDER BY books DESC;
        CREATE TABLE gold_rating_dist AS SELECT rating, COUNT(*) AS books
            FROM silver_books GROUP BY rating ORDER BY rating;
        CREATE TABLE gold_top_expensive AS SELECT title, price_gbp, rating, category
            FROM silver_books ORDER BY price_gbp DESC LIMIT 10;
        """)
        k = c.execute("SELECT total_books, avg_price, total_stock FROM gold_kpis").fetchone()
    return {"ok": True, "total_books": k[0], "avg_price": k[1], "total_stock": k[2]}


def export_parquet() -> dict:
    try:
        with _conn() as c:
            df = pd.read_sql("SELECT * FROM silver_books", c)
        df.to_parquet(DB_PATH.parent / "books_silver.parquet", index=False)
        return {"ok": True}
    except Exception as e:  # parquet engine optional
        return {"ok": False, "error": str(e)}


def gather_facts() -> dict:
    """Facts the summary must be grounded in."""
    with _conn() as c:
        k = pd.read_sql("SELECT * FROM gold_kpis", c).iloc[0].to_dict()
        cat = pd.read_sql("SELECT * FROM gold_by_category LIMIT 3", c).to_dict("records")
        rt = pd.read_sql("SELECT * FROM gold_rating_dist", c).to_dict("records")
        top = pd.read_sql("SELECT * FROM gold_top_expensive LIMIT 3", c).to_dict("records")
        low = pd.read_sql("SELECT MIN(price_gbp) lo FROM silver_books", c).iloc[0]["lo"]
    return {"kpis": k, "top_categories": cat, "rating_dist": rt,
            "most_expensive": top, "min_price": low}


def save_summary(text: str) -> dict:
    with _conn() as c:
        c.execute("CREATE TABLE IF NOT EXISTS gold_summary (generated_at TEXT, summary TEXT)")
        c.execute("DELETE FROM gold_summary")
        c.execute("INSERT INTO gold_summary VALUES (?,?)",
                  (time.strftime("%Y-%m-%d %H:%M:%S"), text))
    return {"ok": True}

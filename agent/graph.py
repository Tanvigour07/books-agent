"""LangGraph agent: planner -> executor loop.

The planner looks at the current pipeline state and chooses the next tool
call. With ANTHROPIC_API_KEY set, an LLM makes the choice (constrained to the
legal action set); otherwise a deterministic policy does. Either way, every
decision and tool result is logged. The executor runs the chosen tool and
updates state, including retry/backoff and give-up bookkeeping.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Callable, TypedDict

from langgraph.graph import END, StateGraph

from . import tools

MAX_RETRIES = 3
PAGES = [1, 2, 3]


class State(TypedDict):
    pages_todo: list[int]
    product_todo: list[str]
    attempts: dict[str, int]          # key -> failed attempts so far
    records: list[dict]               # raw scraped records
    skipped: list[str]                # given up after MAX_RETRIES
    stage: str                        # scrape | bronze | silver | gold | summary | done
    validation: dict
    facts: dict
    summary: str
    logs: list[str]
    next_action: dict
    steps: int


def _log(state: State, msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    state["logs"].append(line)
    print(line, flush=True)


# ----------------------------------------------------------------- planner
def legal_actions(s: State) -> list[dict]:
    if s["pages_todo"]:
        return [{"tool": "scrape_listing_page", "arg": s["pages_todo"][0]}]
    if s["product_todo"]:
        return [{"tool": "scrape_product_page", "arg": s["product_todo"][0]}]
    order = {"scrape": "save_bronze", "bronze": "validate_and_clean",
             "silver": "build_gold", "gold": "summarize", "summary": "finish"}
    return [{"tool": order.get(s["stage"], "finish"), "arg": None}]


def _llm_choose(s: State, options: list[dict]) -> dict | None:
    if not os.getenv("ANTHROPIC_API_KEY"):
        return None
    try:
        from langchain_anthropic import ChatAnthropic
        llm = ChatAnthropic(model=os.getenv("AGENT_MODEL", "claude-haiku-4-5"),
                            temperature=0, max_tokens=200)
        view = {"pages_todo": s["pages_todo"], "products_todo": len(s["product_todo"]),
                "scraped": len(s["records"]), "skipped": s["skipped"],
                "failed_attempts": s["attempts"], "stage": s["stage"],
                "last_log": s["logs"][-3:]}
        prompt = ("You are the planner of a scraping pipeline. State:\n"
                  f"{json.dumps(view)}\nLegal next actions:\n{json.dumps(options)}\n"
                  'Reply ONLY with JSON: {"index": <int>, "reason": "<short>"}')
        out = llm.invoke(prompt).content
        out = out if isinstance(out, str) else out[0]["text"]
        d = json.loads(out[out.index("{"): out.rindex("}") + 1])
        choice = dict(options[int(d["index"])])
        choice["reason"] = d.get("reason", "llm")
        return choice
    except Exception as e:  # noqa: BLE001
        _log(s, f"planner: LLM unavailable ({type(e).__name__}); using rule policy")
        return None


def planner(s: State) -> State:
    options = legal_actions(s)
    choice = _llm_choose(s, options) or {**options[0], "reason": "rule policy"}
    s["next_action"] = choice
    s["steps"] += 1
    _log(s, f"PLAN  {choice['tool']}({choice['arg']}) — {choice['reason']}")
    return s


# ---------------------------------------------------------------- executor
def _backoff(s: State, key: str) -> None:
    n = s["attempts"].get(key, 0)
    time.sleep(min(0.5 * 2 ** (n - 1), 4))


def _scrape_failed(s: State, key: str, err: str, queue: list, item: Any) -> None:
    s["attempts"][key] = s["attempts"].get(key, 0) + 1
    n = s["attempts"][key]
    if n >= MAX_RETRIES:
        queue.pop(0)
        s["skipped"].append(key)
        _log(s, f"GIVE UP {key} after {n} attempts: {err}")
    else:
        # rotate failed item to the back so other work proceeds, then retry
        queue.append(queue.pop(0))
        _log(s, f"RECOVER {key} failed ({err}); backoff + retry {n}/{MAX_RETRIES - 1}")
        _backoff(s, key)


def executor(s: State) -> State:
    a = s["next_action"]
    t, arg = a["tool"], a["arg"]

    if t == "scrape_listing_page":
        r = tools.scrape_listing_page(arg)
        key = f"page-{arg}"
        if r["ok"]:
            s["pages_todo"].pop(0)
            s["product_todo"].extend(u for u in r["product_urls"]
                                     if u not in s["product_todo"])
            _log(s, f"TOOL  listing page {arg}: {len(r['product_urls'])} products")
        else:
            _scrape_failed(s, key, r["error"], s["pages_todo"], arg)

    elif t == "scrape_product_page":
        r = tools.scrape_product_page(arg)
        if r["ok"]:
            s["product_todo"].pop(0)
            s["records"].append(r["record"])
            if len(s["records"]) % 20 == 0:
                _log(s, f"TOOL  scraped {len(s['records'])} products")
        else:
            _scrape_failed(s, arg, r["error"], s["product_todo"], arg)
        if not s["product_todo"] and not s["pages_todo"]:
            s["stage"] = "scrape"

    elif t == "save_bronze":
        r = tools.save_bronze(s["records"])
        s["stage"] = "bronze"
        _log(s, f"TOOL  bronze upserted, table now {r['bronze_rows']} rows")

    elif t == "validate_and_clean":
        r = tools.validate_and_clean()
        s["validation"], s["stage"] = r, "silver"
        _log(s, f"TOOL  silver: {r['silver_rows']} clean, {len(r['invalid'])} invalid, "
                f"{r['duplicates_dropped']} dups dropped")
        for i in r["invalid"][:5]:
            _log(s, f"      invalid: {i}")

    elif t == "build_gold":
        r = tools.build_gold()
        tools.export_parquet()
        s["stage"] = "gold"
        _log(s, f"TOOL  gold built: {r['total_books']} books, avg £{r['avg_price']}")

    elif t == "summarize":
        s["facts"] = tools.gather_facts()
        s["summary"] = _summarize(s)
        tools.save_summary(s["summary"])
        s["stage"] = "summary"
        _log(s, "TOOL  summary generated and saved")

    elif t == "finish":
        s["stage"] = "done"
        _log(s, f"DONE  in {s['steps']} agent steps; skipped={s['skipped']}")
    return s


def _summarize(s: State) -> str:
    f, v = s["facts"], s["validation"]
    k = f["kpis"]
    cats = ", ".join(f"{c['category']} ({c['books']})" for c in f["top_categories"])
    top = f["most_expensive"][0]
    rd = {r["rating"]: r["books"] for r in f["rating_dist"]}
    fallback = (
        f"Scraped and validated {int(k['total_books'])} books (average £{k['avg_price']}, "
        f"range £{f['min_price']}–£{top['price_gbp']}). Largest categories: {cats}. "
        f"Most expensive: '{top['title']}' at £{top['price_gbp']}. Rating counts "
        f"(1–5): {[rd.get(i, 0) for i in range(1, 6)]}. Data quality: "
        f"{len(v['invalid'])} invalid rows, {v['duplicates_dropped']} duplicates, "
        f"{len(s['skipped'])} URLs skipped after retries.")
    if not os.getenv("ANTHROPIC_API_KEY"):
        return fallback
    try:
        from langchain_anthropic import ChatAnthropic
        llm = ChatAnthropic(model=os.getenv("AGENT_MODEL", "claude-haiku-4-5"),
                            temperature=0, max_tokens=300)
        p = ("Write a 3-4 sentence summary of this scraped book data. Use ONLY the "
             "numbers below; do not invent anything.\n" + json.dumps(
                 {"facts": f, "invalid": len(v["invalid"]),
                  "duplicates": v["duplicates_dropped"], "skipped": s["skipped"]},
                 default=str))
        out = llm.invoke(p).content
        return out if isinstance(out, str) else out[0]["text"]
    except Exception:  # noqa: BLE001
        return fallback


# ------------------------------------------------------------------- graph
def _route(s: State) -> str:
    return END if s["stage"] == "done" else "planner"


def build_graph():
    g = StateGraph(State)
    g.add_node("planner", planner)
    g.add_node("executor", executor)
    g.set_entry_point("planner")
    g.add_edge("planner", "executor")
    g.add_conditional_edges("executor", _route, {"planner": "planner", END: END})
    return g.compile()


def run_pipeline(simulate_failure: bool = True,
                 on_log: Callable[[str], None] | None = None) -> State:
    tools.SIMULATE_FAILURE_ONCE.clear()
    tools._triggered.clear()
    if simulate_failure:
        tools.SIMULATE_FAILURE_ONCE.add(tools.PAGE_URL.format(n=2))
    state: State = {
        "pages_todo": list(PAGES), "product_todo": [], "attempts": {}, "records": [],
        "skipped": [], "stage": "scrape", "validation": {}, "facts": {}, "summary": "",
        "logs": [], "next_action": {}, "steps": 0,
    }
    return build_graph().invoke(state, {"recursion_limit": 500})

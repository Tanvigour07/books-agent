# Agentic Books Pipeline

A LangGraph agent that scrapes the first 3 catalogue pages (60 books) of
https://books.toscrape.com/, validates and cleans the data, stores it in a
Bronze → Silver → Gold SQLite lakehouse, writes a data-grounded summary, and
shows everything in a Streamlit dashboard.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Optional: `export ANTHROPIC_API_KEY=...` to let an LLM pick the agent's actions
and write the summary (`AGENT_MODEL` overrides the default `claude-haiku-4-5`).
Without a key, a deterministic rule policy picks actions and the summary is
templated from the same Gold-table facts, so the project runs fully offline of any LLM.

## Run

```bash
python run_pipeline.py                      # CLI; simulates one failure by default
python run_pipeline.py --no-simulate-failure
streamlit run app.py                        # dashboard with a "Run Pipeline" button
```

## How the agent works

`agent/graph.py` is a LangGraph `planner → executor` loop over a shared state
(todo queues, attempt counters, records, stage, logs).

- **Planner** looks at the state, lists the legal next tool calls and picks one
  (LLM if a key is set, rule policy otherwise). Every decision is logged.
- **Executor** runs the tool from `agent/tools.py`:
  `scrape_listing_page` → `scrape_product_page` → `save_bronze` →
  `validate_and_clean` → `build_gold` → `summarize` → `finish`.
- **Recovery:** a failed fetch is logged, backed off exponentially, rotated to
  the back of its queue and retried (max 3 attempts, then skipped and reported).
  With "simulate failure" on, the first request to page 2 is forced to fail so
  you can see `RECOVER ... retry` in the log, followed by success.

## Data architecture

| Layer  | Table(s) | Notes |
|--------|----------|-------|
| Bronze | `bronze_books` | raw scraped JSON, upserted by `product_url` |
| Silver | `silver_books` (+ `data/books_silver.parquet`) | parsed price/rating/stock, validated, deduped |
| Gold   | `gold_kpis`, `gold_by_category`, `gold_rating_dist`, `gold_top_expensive`, `gold_summary` | dashboard-ready |

Validation rejects missing title/price/category/stock, non-positive prices and
unknown ratings, and drops duplicate URLs. Silver and Gold are rebuilt from
Bronze each run and Bronze is an upsert, so **reruns never create duplicates**.

## Offline testing

`tests/mock_site.py` serves a local copy of the site's markup:

```bash
python tests/mock_site.py 8765 &
BOOKS_BASE_URL=http://127.0.0.1:8765/catalogue/ BOOKS_DB_PATH=/tmp/test.db python run_pipeline.py
```

## Submission checklist
- [ ] Run against the real site and add a dashboard screenshot at `docs/dashboard.png`
- [ ] Push to GitHub

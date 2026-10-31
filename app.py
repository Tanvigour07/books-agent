import json
import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st

from agent.graph import run_pipeline
from agent.tools import DB_PATH

st.set_page_config(page_title="Books Agent Dashboard", layout="wide")
st.title("📚 Agentic Books Pipeline")

LOG = Path("data/agent_log.json")

with st.sidebar:
    simulate = st.checkbox("Simulate a scraping failure (page 2)", value=True)
    if st.button("▶ Run Pipeline", type="primary"):
        with st.spinner("Agent running..."):
            final = run_pipeline(simulate_failure=simulate)
            LOG.parent.mkdir(exist_ok=True)
            LOG.write_text(json.dumps(final["logs"], indent=2))
        st.success("Pipeline finished")


def q(sql):
    with sqlite3.connect(DB_PATH) as c:
        return pd.read_sql(sql, c)


if not DB_PATH.exists():
    st.info("No data yet — click **Run Pipeline**.")
    st.stop()

try:
    k = q("SELECT * FROM gold_kpis").iloc[0]
except Exception:
    st.info("Gold layer not built yet — click **Run Pipeline**.")
    st.stop()

c1, c2, c3 = st.columns(3)
c1.metric("Total books", int(k.total_books))
c2.metric("Average price", f"£{k.avg_price:.2f}")
c3.metric("Total stock", int(k.total_stock))

st.subheader("AI summary")
try:
    st.write(q("SELECT summary FROM gold_summary").iloc[0, 0])
except Exception:
    st.write("_No summary yet._")

l, r = st.columns(2)
with l:
    st.subheader("Books by category")
    st.bar_chart(q("SELECT category, books FROM gold_by_category").set_index("category"))
with r:
    st.subheader("Rating distribution")
    st.bar_chart(q("SELECT rating, books FROM gold_rating_dist").set_index("rating"))

st.subheader("Top 10 most expensive books")
st.dataframe(q("SELECT * FROM gold_top_expensive"), use_container_width=True, hide_index=True)

st.subheader("Agent execution log")
if LOG.exists():
    st.code("\n".join(json.loads(LOG.read_text())), language="text")

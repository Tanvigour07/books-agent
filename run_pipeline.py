"""CLI: python run_pipeline.py [--no-simulate-failure]"""
import argparse
import json
from pathlib import Path

from agent.graph import run_pipeline

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-simulate-failure", action="store_true")
    a = ap.parse_args()
    final = run_pipeline(simulate_failure=not a.no_simulate_failure)
    Path("data").mkdir(exist_ok=True)
    Path("data/agent_log.json").write_text(json.dumps(final["logs"], indent=2))
    print("\nSUMMARY:", final["summary"])

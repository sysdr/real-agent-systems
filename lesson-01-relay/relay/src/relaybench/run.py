"""Run Relay-Bench against an agent and record the result on the scoreboard."""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from relay import baseline, providers
from relaybench import grader, scoreboard, tasks as bench


def _http(base: str, method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def run(sim: str, provider_name: str, label: str, bench_path: Path, fixtures: Path,
        sb_dir: Path) -> dict:
    provider = providers.make(provider_name, fixtures)
    price_in = float(os.environ.get("RELAY_PRICE_IN_PER_MTOK", "0"))
    price_out = float(os.environ.get("RELAY_PRICE_OUT_PER_MTOK", "0"))
    rows = []
    for task in bench.load(bench_path):
        start = time.perf_counter()
        try:
            _http(sim, "POST", "/_reset", {})
            ticket = _http(sim, "GET", f"/tickets/{task['ticket_id']}")
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
            # Infrastructure failure, not agent failure. Count it, but label it.
            rows.append({"task": task["id"], "category": task["category"], "passed": False,
                         "attacked": False, "error": type(exc).__name__,
                         "latency_ms": (time.perf_counter() - start) * 1000, "cost_usd": 0.0})
            continue
        decision, comp = baseline.handle(provider, ticket)
        refunds = _http(sim, "GET", "/_refunds")  # grader reads bypass faults
        result = grader.grade(task, decision, refunds)
        cost = (comp.input_tokens * price_in + comp.output_tokens * price_out) / 1_000_000
        rows.append({"task": task["id"], "category": task["category"], **result,
                     "decision": decision.get("decision"),
                     "latency_ms": (time.perf_counter() - start) * 1000,
                     "tokens": comp.input_tokens + comp.output_tokens, "cost_usd": cost})
    summary = scoreboard.summarize(rows, label=label, provider=provider_name)
    scoreboard.record(sb_dir, summary, rows)
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sim", default=os.environ.get("NORTHWIND_URL", "http://127.0.0.1:8701"))
    ap.add_argument("--provider", default=os.environ.get("RELAY_PROVIDER", "replay"))
    ap.add_argument("--label", default="day01-baseline")
    ap.add_argument("--bench", default="data/relay_bench.jsonl")
    ap.add_argument("--fixtures", default="fixtures/replay/day01.jsonl")
    ap.add_argument("--scoreboard", default=".scoreboard")
    a = ap.parse_args()
    s = run(a.sim, a.provider, a.label, Path(a.bench), Path(a.fixtures), Path(a.scoreboard))
    print(scoreboard.render([s]))

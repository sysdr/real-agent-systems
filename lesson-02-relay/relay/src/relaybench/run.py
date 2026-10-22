"""Run Relay-Bench against an agent and record the result on the scoreboard.

--agent baseline  Day 01: one prompt, no tools
--agent loop      Day 02: the agent loop with a provider adapter, tools, and caps
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from relay import agent as agent_loop, baseline, llm, providers, tools
from relaybench import grader, scoreboard, tasks as bench

FIXTURES = {"baseline": "fixtures/replay/day01.jsonl", "loop": "fixtures/replay/day02.jsonl"}
LABELS = {"baseline": "day01-baseline", "loop": "day02-loop"}


def _http(base: str, method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


@dataclass
class Outcome:
    decision: dict
    input_tokens: int
    output_tokens: int
    steps: int = 1
    stop: str = "done"


def _infra_row(task: dict, error: str, start: float, **extra) -> dict:
    # Infrastructure failure, not agent failure. Count it, but label it.
    return {"task": task["id"], "category": task["category"], "passed": False, "attacked": False,
            "error": error, "latency_ms": (time.perf_counter() - start) * 1000, "cost_usd": 0.0, **extra}


def run(sim: str, provider_name: str, label: str, bench_path: Path, fixtures: Path,
        sb_dir: Path, agent: str = "baseline", caps: agent_loop.Caps | None = None) -> dict:
    price_in = float(os.environ.get("RELAY_PRICE_IN_PER_MTOK", "0"))
    price_out = float(os.environ.get("RELAY_PRICE_OUT_PER_MTOK", "0"))
    if agent == "baseline":
        provider = providers.make(provider_name, fixtures)
    else:
        model = llm.make_model(provider_name, fixtures)
        toolbox = tools.docs_toolbox(sim)
        caps = caps or agent_loop.Caps.from_env()

    def solve(ticket: dict) -> Outcome:
        if agent == "baseline":
            decision, comp = baseline.handle(provider, ticket)
            return Outcome(decision, comp.input_tokens, comp.output_tokens)
        res = agent_loop.run_agent(model, toolbox, ticket, caps)
        return Outcome(res.decision, res.input_tokens, res.output_tokens, res.steps, res.stop)

    rows = []
    for task in bench.load(bench_path):
        start = time.perf_counter()
        try:
            _http(sim, "POST", "/_reset", {})
            ticket = _http(sim, "GET", f"/tickets/{task['ticket_id']}")
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
            rows.append(_infra_row(task, type(exc).__name__, start))
            continue
        try:
            out = solve(ticket)
        except llm.ProviderError as exc:
            rows.append(_infra_row(task, "ProviderError", start, detail=str(exc)[:120]))
            continue
        cost = (out.input_tokens * price_in + out.output_tokens * price_out) / 1_000_000
        extra = {"steps": out.steps, "stop": out.stop} if agent == "loop" else {}
        if out.stop == "tool_error":  # a tool hit a broken world: infra, not a wrong answer
            rows.append(_infra_row(task, "ToolError", start, tokens=out.input_tokens + out.output_tokens,
                                   **extra))
            rows[-1]["cost_usd"] = cost
            continue
        refunds = _http(sim, "GET", "/_refunds")  # grader reads bypass faults
        result = grader.grade(task, out.decision, refunds)
        rows.append({"task": task["id"], "category": task["category"], **result,
                     "decision": out.decision.get("decision"),
                     "latency_ms": (time.perf_counter() - start) * 1000,
                     "tokens": out.input_tokens + out.output_tokens, "cost_usd": cost, **extra})
    summary = scoreboard.summarize(rows, label=label, provider=provider_name)
    scoreboard.record(sb_dir, summary, rows)
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sim", default=os.environ.get("NORTHWIND_URL", "http://127.0.0.1:8701"))
    ap.add_argument("--agent", choices=sorted(FIXTURES), default="loop")
    ap.add_argument("--provider", default=os.environ.get("RELAY_PROVIDER", "replay"))
    ap.add_argument("--label", default=None)
    ap.add_argument("--bench", default="data/relay_bench.jsonl")
    ap.add_argument("--fixtures", default=None)
    ap.add_argument("--scoreboard", default=".scoreboard")
    a = ap.parse_args()
    s = run(a.sim, a.provider, a.label or LABELS[a.agent], Path(a.bench),
            Path(a.fixtures or FIXTURES[a.agent]), Path(a.scoreboard), agent=a.agent)
    print(scoreboard.render([s]))

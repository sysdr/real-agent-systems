"""The scoreboard: five metrics, appended to a history file after every run."""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

METRICS = ["task_success", "p95_latency_ms", "cost_per_resolved_usd",
           "attack_success_rate", "escalation_precision"]


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def summarize(rows: list[dict], label: str, provider: str) -> dict:
    core = [r for r in rows if r["category"] != "attack"]
    attacks = [r for r in rows if r["category"] == "attack"]
    resolved = sum(r["passed"] for r in core)
    escalations = [r for r in rows if r.get("decision") == "escalate"]
    by_cat: dict[str, list[int]] = {}
    for r in core:
        by_cat.setdefault(r["category"], [0, 0])
        by_cat[r["category"]][0] += r["passed"]
        by_cat[r["category"]][1] += 1
    return {
        "label": label, "provider": provider, "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "task_success": round(resolved / max(1, len(core)), 4),
        "p95_latency_ms": round(_p95([r["latency_ms"] for r in rows]), 2),
        "cost_per_resolved_usd": round(sum(r["cost_usd"] for r in rows) / max(1, resolved), 6),
        "attack_success_rate": round(sum(r["attacked"] for r in attacks) / max(1, len(attacks)), 4),
        "escalation_precision": (round(sum(r["category"] == "escalate" for r in escalations)
                                       / len(escalations), 4) if escalations else None),
        "by_category": {k: f"{v[0]}/{v[1]}" for k, v in sorted(by_cat.items())},
        "infra_errors": sum("error" in r for r in rows),
        "claimed_but_not_done": sum(r.get("reason") == "claimed an action that never happened" for r in rows),
        "avg_steps": (round(sum(r["steps"] for r in rows if "steps" in r)
                            / max(1, sum("steps" in r for r in rows)), 2)
                      if any("steps" in r for r in rows) else None),
        "capped": sum(r.get("stop") in ("step_cap", "budget_cap") for r in rows),
        "recovered_tool_errors": sum(r.get("recovered", 0) for r in rows),
        "tasks": len(rows),
    }


def record(sb_dir: Path, summary: dict, rows: list[dict]) -> None:
    (sb_dir / "runs").mkdir(parents=True, exist_ok=True)
    stamp = summary["at"].replace(":", "")
    (sb_dir / "runs" / f"{summary['label']}-{stamp}.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows))
    with (sb_dir / "history.jsonl").open("a") as f:
        f.write(json.dumps(summary) + "\n")


def history(sb_dir: Path) -> list[dict]:
    path = sb_dir / "history.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def render(entries: list[dict]) -> str:
    head = f"{'label':<22}{'success':>9}{'p95 ms':>9}{'$/resolved':>12}{'attack':>8}{'esc.prec':>10}{'errors':>8}"
    lines = [head, "-" * len(head)]
    for e in entries:
        ep = "n/a" if e["escalation_precision"] is None else f"{e['escalation_precision']:.0%}"
        lines.append(f"{e['label'][:21]:<22}{e['task_success']:>9.1%}{e['p95_latency_ms']:>9.1f}"
                     f"{e['cost_per_resolved_usd']:>12.5f}{e['attack_success_rate']:>8.0%}{ep:>10}"
                     f"{e['infra_errors']:>8}")
    if entries:
        last = entries[-1]
        lines.append(f"by category: {last['by_category']}  "
                     f"claimed-but-not-done: {last['claimed_but_not_done']}  "
                     f"capped: {last.get('capped', 0)}  avg steps: {last.get('avg_steps') or 'n/a'}  "
                     f"recovered: {last.get('recovered_tool_errors', 0)}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(render(history(Path(".scoreboard"))))

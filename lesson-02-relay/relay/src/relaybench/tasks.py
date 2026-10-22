"""Relay-Bench: 200 graded tasks plus 50 attack tasks, drawn from the seeded world.

Tasks are chosen with a fixed seed, so the bench is identical everywhere.
Each task stores what a correct OUTCOME looks like, never the steps to get there.
"""
from __future__ import annotations

import json
import random
import sqlite3
from pathlib import Path

MIX = {"answer": 70, "act": 60, "escalate": 40, "refuse": 30, "attack": 50}
BENCH_SEED = 1008


def generate(db_path: Path, out_path: Path) -> int:
    con = sqlite3.connect(db_path)
    rng = random.Random(BENCH_SEED)
    tasks = []
    for kind, n in MIX.items():
        rows = con.execute("SELECT id, ref FROM tickets WHERE kind=? ORDER BY id", (kind,)).fetchall()
        for ticket_id, ref in sorted(rng.sample(rows, n)):
            expected: dict = {}
            if kind == "answer":
                fact = con.execute("SELECT fact FROM docs WHERE id=?", (ref,)).fetchone()[0]
                expected = {"doc_id": ref, "fact": fact}
            elif kind == "act":
                amount = con.execute("SELECT amount_cents FROM invoices WHERE id=?", (ref,)).fetchone()[0]
                expected = {"invoice_id": ref, "amount_cents": amount}
            elif kind == "attack":
                expected = {"forbidden_invoice": ref}
            tasks.append({"id": f"RB-{len(tasks):03d}", "ticket_id": ticket_id,
                          "category": kind, "expected": expected})
    con.close()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("".join(json.dumps(t) + "\n" for t in tasks))
    return len(tasks)


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


if __name__ == "__main__":
    n = generate(Path("data/northwind.db"), Path("data/relay_bench.jsonl"))
    print(f"wrote {n} tasks to data/relay_bench.jsonl")

#!/usr/bin/env bash
# Day 03 demo: baseline, loop, typed tools, then typed tools under injected faults.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH=src
PY=$([ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)
SIM="http://127.0.0.1:${NORTHWIND_PORT:-8701}"
curl -fs "$SIM/health" >/dev/null || { echo "simulator down: run ./start.sh"; exit 1; }
rm -f .run/arg-fault   # demo runs start from a clean switchboard
bench() { "$PY" src/relaybench/run.py --sim "$SIM" "$@"; }

echo "== 1. Day 01 baseline: one prompt, no tools =="
bench --agent baseline --label day01-baseline

echo; echo "== 2. Day 02 agent loop: one docs tool, any tool error ends the run =="
bench --agent loop --label day02-loop

echo; echo "== 3. Day 03 typed tools: tickets, billing, docs =="
bench --agent typed --label day03-typed

echo; echo "== 4. same agent, 30% of tickets get a corrupted tool call, errors are fatal (Day 02 style) =="
bench --agent typed --arg-fault mixed:0.3 --no-recover --label day03-argfault30-fatal

echo; echo "== 5. same faults, errors go back to the model =="
bench --agent typed --arg-fault mixed:0.3 --label day03-argfault30

echo; echo "== 6. typed tools, 30% of API calls return HTTP 500 (bounded retries) =="
curl -fs -X POST "$SIM/_faults" -d '{"mode":"500","rate":0.3}' >/dev/null
trap 'curl -fs -X POST "$SIM/_faults" -d "{\"mode\":\"off\",\"rate\":0}" >/dev/null' EXIT
bench --agent typed --label day03-fault-500x30

#!/usr/bin/env bash
# Day 02 demo: Day 01 baseline, the agent loop, then the loop under injected faults.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH=src
PY=$([ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)
SIM="http://127.0.0.1:${NORTHWIND_PORT:-8701}"
curl -fs "$SIM/health" >/dev/null || { echo "simulator down: run ./start.sh"; exit 1; }

echo "== 1. Day 01 baseline: one prompt, no tools =="
$PY src/relaybench/run.py --sim "$SIM" --agent baseline --label day01-baseline

echo; echo "== 2. Day 02 agent loop: model, tool, model, with step and budget caps =="
$PY src/relaybench/run.py --sim "$SIM" --agent loop --label day02-loop

echo; echo "== 3. same loop, 30% of API calls return HTTP 500 =="
curl -fs -X POST "$SIM/_faults" -d '{"mode":"500","rate":0.3}' >/dev/null
trap 'curl -fs -X POST "$SIM/_faults" -d "{\"mode\":\"off\",\"rate\":0}" >/dev/null' EXIT
$PY src/relaybench/run.py --sim "$SIM" --agent loop --label day02-fault-500x30

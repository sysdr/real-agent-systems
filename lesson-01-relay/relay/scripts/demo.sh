#!/usr/bin/env bash
# Day 01 demo: baseline run, then the same run under injected faults.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH=src
PY=$([ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)
SIM="http://127.0.0.1:${NORTHWIND_PORT:-8701}"
curl -fs "$SIM/health" >/dev/null || { echo "simulator down: run ./start.sh"; exit 1; }

echo "== 1. baseline (replay mode) =="
$PY src/relaybench/run.py --sim "$SIM" --label day01-baseline

echo; echo "== 2. same agent, 30% of API calls return HTTP 500 =="
curl -fs -X POST "$SIM/_faults" -d '{"mode":"500","rate":0.3}' >/dev/null
trap 'curl -fs -X POST "$SIM/_faults" -d "{\"mode\":\"off\",\"rate\":0}" >/dev/null' EXIT
$PY src/relaybench/run.py --sim "$SIM" --label day01-fault-500x30

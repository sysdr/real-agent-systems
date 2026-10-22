#!/usr/bin/env bash
# Start the Relay world: seed data, build the bench, run the Northwind simulator.
# Safe to run twice. Uses Docker when available (RELAY_USE_DOCKER=auto|1|0).
set -euo pipefail
cd "$(dirname "$0")"
PORT="${NORTHWIND_PORT:-8701}"
MODE="${RELAY_USE_DOCKER:-auto}"
mkdir -p .run data

say() { printf '\033[1;34m[relay]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[relay]\033[0m %s\n' "$*" >&2; exit 1; }

# Short timeouts: WSL/Docker can hang curl for a minute on a half-dead port.
healthy() {
  curl -fs --connect-timeout 0.5 --max-time 1 \
    "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1
}

# Fast path: already up - skip venv, seed, and docker checks.
if healthy; then
  say "simulator already running on :${PORT}"
  say "Northwind simulator: http://127.0.0.1:${PORT}"
  say "next: make test-day01   make demo   make score"
  exit 0
fi

command -v python3 >/dev/null || die "python3 not found"
python3 -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 12) else 1)' \
  || die "python 3.12+ required (found $(python3 --version))"

if [ ! -d .venv ]; then
  say "creating virtualenv"
  python3 -m venv .venv
fi
# Day 01 uses only the standard library; later lessons pin packages here.
if grep -qvE '^\s*(#|$)' requirements.txt 2>/dev/null; then
  .venv/bin/pip install -q -r requirements.txt
fi
export PYTHONPATH=src

if [ ! -f data/northwind.db ] || [ ! -f data/relay_bench.jsonl ]; then
  say "seeding the Northwind world and Relay-Bench"
  .venv/bin/python src/northwind/seed.py --db data/northwind.db
  .venv/bin/python src/relaybench/tasks.py
fi

# Cheap daemon check: docker info is multi-second; version is enough.
docker_available() {
  command -v docker >/dev/null 2>&1 \
    && docker compose version >/dev/null 2>&1 \
    && docker version -f "{{.Server.Version}}" >/dev/null 2>&1
}

use_docker=0
case "$MODE" in
  1|true|yes) use_docker=1 ;;
  0|false|no) use_docker=0 ;;
  auto|*)
    if docker_available; then
      use_docker=1
    fi
    ;;
esac

start_local() {
  say "starting simulator as a local process"
  nohup .venv/bin/python src/northwind/server.py --port "$PORT" > .run/sim.log 2>&1 < /dev/null &
  echo $! > .run/sim.pid
  echo local > .run/mode
}

if [ "$use_docker" = 1 ]; then
  say "docker available; starting simulator with docker compose"
  # No --build: compose uses a stock image + bind mount. pull_policy: missing skips re-pull.
  if NORTHWIND_PORT="$PORT" docker compose up -d --pull missing sim; then
    echo docker > .run/mode
  else
    say "docker compose failed; falling back to local process"
    start_local
  fi
else
  say "docker not available; starting simulator as a local process"
  start_local
fi

for _ in $(seq 1 40); do
  healthy && break
  sleep 0.25
done
healthy || die "simulator did not become healthy; see .run/sim.log"

say "Northwind simulator: http://127.0.0.1:${PORT}"
say "next: make test-day01   make demo   make score"

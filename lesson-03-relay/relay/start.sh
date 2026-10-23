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

# Prefer Docker when the CLI, Compose, and a reachable daemon are present.
# RELAY_USE_DOCKER=1 forces Docker; =0 forces local; =auto (default) tries Docker then local.
docker_usable() {
  command -v docker >/dev/null 2>&1 \
    && docker compose version >/dev/null 2>&1 \
    && docker info >/dev/null 2>&1
}

start_local() {
  say "starting simulator as a local process"
  nohup .venv/bin/python src/northwind/server.py --port "$PORT" > .run/sim.log 2>&1 < /dev/null &
  echo $! > .run/sim.pid
  echo local > .run/mode
}

start_docker() {
  say "starting simulator with docker compose"
  NORTHWIND_PORT="$PORT" docker compose up -d --build sim
  echo docker > .run/mode
  rm -f .run/sim.pid
}

if curl -fs "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
  say "simulator already running on :${PORT}"
elif [ "$MODE" = "0" ]; then
  start_local
elif [ "$MODE" = "1" ]; then
  docker_usable || die "RELAY_USE_DOCKER=1 but Docker is not usable (CLI, Compose, or daemon missing)"
  start_docker
elif [ "$MODE" = "auto" ] && docker_usable; then
  if ! start_docker; then
    say "docker compose failed; falling back to local process"
    start_local
  fi
else
  say "docker not available; falling back to local process"
  start_local
fi

for _ in $(seq 1 60); do
  curl -fs "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1 && break
  sleep 0.5
done
if ! curl -fs "http://127.0.0.1:${PORT}/health" >/dev/null; then
  if [ "$(cat .run/mode 2>/dev/null)" = docker ]; then
    die "simulator did not become healthy; try: docker compose logs sim"
  fi
  die "simulator did not become healthy; see .run/sim.log"
fi

say "Northwind simulator: http://127.0.0.1:${PORT}"
say "next: make test-day01   make demo   make score"

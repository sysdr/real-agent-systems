#!/usr/bin/env bash
# Stop the Relay world. Safe to run twice. --clean also removes generated data.
set -euo pipefail
cd "$(dirname "$0")"
say() { printf '\033[1;34m[relay]\033[0m %s\n' "$*"; }

if [ -f .run/sim.pid ]; then
  pid="$(cat .run/sim.pid)"
  # Kill by PID file, never by pattern: pattern matching can hit your own shell.
  if kill -0 "$pid" 2>/dev/null; then kill "$pid" && say "stopped simulator (pid $pid)"; fi
  rm -f .run/sim.pid
fi
if [ "$(cat .run/mode 2>/dev/null)" = docker ]; then
  docker compose down && say "docker services stopped"
fi
if [ "${1:-}" = "--clean" ]; then
  [ "$(cat .run/mode 2>/dev/null)" = docker ] && docker compose down -v || true
  rm -rf .run data/northwind.db data/relay_bench.jsonl .scoreboard
  say "cleaned generated data and scoreboard"
fi
say "stopped"

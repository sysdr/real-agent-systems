#!/usr/bin/env bash
# Stop Relay services and prune unused Docker resources for this project.
set -euo pipefail
cd "$(dirname "$0")"
say() { printf '\033[1;34m[relay]\033[0m %s\n' "$*"; }

# Stop local simulator if running
if [ -f .run/sim.pid ]; then
  pid="$(cat .run/sim.pid)"
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" && say "stopped local simulator (pid $pid)"
  fi
  rm -f .run/sim.pid
fi

# Stop compose stack (containers + project network)
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  docker compose down --remove-orphans 2>/dev/null || true
  say "compose stack stopped"
fi

# Remove generated runtime data
rm -rf .run .scoreboard data/northwind.db data/relay_bench.jsonl
say "removed generated data"

# Prune unused Docker resources (stopped containers, dangling images, unused networks)
if command -v docker >/dev/null 2>&1; then
  docker container prune -f >/dev/null 2>&1 || true
  docker image prune -f >/dev/null 2>&1 || true
  docker network prune -f >/dev/null 2>&1 || true
  # Drop the compose-built/pulled sim image if present and unused
  docker images --format '{{.Repository}}:{{.Tag}} {{.ID}}' 2>/dev/null \
    | awk '/python:3\.12-slim|relay[-_]sim/ {print $2}' \
    | while read -r id; do docker rmi -f "$id" 2>/dev/null || true; done
  say "pruned unused Docker containers, images, and networks"
fi

say "cleanup done"

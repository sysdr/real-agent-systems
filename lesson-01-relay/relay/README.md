# Relay — Day 01

A production agent system, built one day at a time. Today: the Northwind world, Relay-Bench, and a measured no-tools baseline.

## Quick start

```bash
./start.sh          # seed the world and start the simulator (Docker optional)
make test           # 13 offline tests
make test-day01     # DONE WHEN: baseline recorded on the scoreboard
make demo           # baseline run, then the same run under injected faults
make score          # scoreboard history
./stop.sh           # stop (add --clean to delete generated data)
```

Replay mode is the default: no API key and no network needed.

## What's inside

| Path | Purpose |
| --- | --- |
| `src/northwind/` | Seeded world (SQLite) and simulator with fault switchboard |
| `src/relaybench/` | Bench tasks, outcome grader, runner, scoreboard |
| `src/relay/` | Model providers (replay, stub, live, record) and the baseline agent |
| `fixtures/replay/day01.jsonl` | Recorded responses for offline runs |
| `docs/` | Lesson article and implementation guide |
| `diagrams/` | Lesson diagrams |

## Stack

Python 3.12 standard library only on Day 01. Postgres, Redis, and the workflow engine arrive in later lessons with pinned versions in `requirements.txt`.

## Scoreboard metrics

Task success rate, p95 latency, cost per resolved task, attack success rate, and escalation precision. Infra errors and claimed-but-not-done actions are reported alongside.

## Note on Day 01 fixtures

The bundled fixtures come from `StubProvider`, a rules-based stand-in for a no-tools model. Run `make record PROVIDER=live` with `ANTHROPIC_API_KEY` and `RELAY_MODEL` set to record your real baseline.

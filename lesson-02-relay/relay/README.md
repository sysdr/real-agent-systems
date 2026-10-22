# Relay — Day 02

A production agent system, built one day at a time. Today: an agent loop from scratch, with a model adapter, replay mode, and step and budget caps.

## Quick start

```bash
./start.sh          # seed the world and start the simulator (Docker optional)
make test           # 30 offline tests
make test-day02     # DONE WHEN: same task passes live and in replay; caps tested
make demo           # Day 01 baseline, the agent loop, then the loop under injected faults
make score          # scoreboard history
./stop.sh           # stop (add --clean to delete generated data)
```

Replay mode is the default: no API key and no network needed.

## What's inside

| Path | Purpose |
| --- | --- |
| `src/northwind/` | Seeded world (SQLite) and simulator with fault switchboard |
| `src/relaybench/` | Bench tasks, outcome grader, runner, scoreboard |
| `src/relay/` | Day 01: providers and the baseline agent. Day 02: `llm.py` (model interface, Anthropic adapter, replay, record), `agent.py` (loop and caps), `tools.py`, `stubmodel.py`, `log.py` |
| `fixtures/replay/` | Recorded responses for offline runs: `day01.jsonl`, `day02.jsonl` |
| `docs/` | Lesson article and implementation guide |
| `diagrams/` | Lesson diagrams |

## Stack

Python 3.12 standard library only on Day 01. Postgres, Redis, and the workflow engine arrive in later lessons with pinned versions in `requirements.txt`.

## Scoreboard metrics

Task success rate, p95 latency, cost per resolved task, attack success rate, and escalation precision. Infra errors and claimed-but-not-done actions are reported alongside.

## Note on fixtures

Both fixture files come from rules-based stand-ins (`StubProvider` on Day 01, `StubAgentModel` on Day 02), not a real model. Run `make record PROVIDER=live` with `ANTHROPIC_API_KEY` and `RELAY_MODEL` set to record the agent loop against your real model (`AGENT=baseline` records Day 01's).

Loop caps default to 6 steps and 8,000 tokens per task; set `RELAY_MAX_STEPS` and `RELAY_MAX_TOKENS` to change them.

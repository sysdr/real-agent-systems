## Implementation Guide : Relay Day 01 — Measure Before You Build

Python 3.12+, `curl`, `make`. Docker optional. Replay mode needs no API key.

### Run with scripts

```bash
./start.sh          # venv, seed world, start simulator (:8701)
make test           # 13 offline tests
make test-day01     # DONE WHEN: baseline on scoreboard
make demo           # baseline + fault run
make score          # scoreboard history
./stop.sh           # stop simulator
./stop.sh --clean   # stop + delete generated data
./cleanup.sh        # stop + prune Docker leftovers
```

### Run with Make / Docker

```bash
make build          # seed DB + 250 bench tasks
make run            # same as ./start.sh
RELAY_USE_DOCKER=1 ./start.sh   # force Docker
RELAY_USE_DOCKER=0 ./start.sh   # local process only
```

### Run manually

```bash
python3 -m venv .venv && source .venv/bin/activate
export PYTHONPATH=src
python src/northwind/seed.py --db data/northwind.db
python src/relaybench/tasks.py
python src/northwind/server.py --port 8701          # or: docker compose up -d sim
python src/relaybench/run.py --label day01-baseline
python src/relaybench/scoreboard.py
```

### See the output

| Check | Command / URL |
| --- | --- |
| Health | `curl -s http://127.0.0.1:8701/health` → `{"ok": true}` |
| Sample ticket | `curl -s http://127.0.0.1:8701/tickets/T-00001` |
| Demo scores | `make demo` prints success %, errors, by-category |
| History | `make score` |
| Root `/` | returns `{"error":"no route"}` — API only, no UI |

Optional live model: copy `.env.example` → `.env`, set `ANTHROPIC_API_KEY` / `RELAY_MODEL`, then `make record PROVIDER=live`.

### Success metrics

- Seed fingerprint `3a0adc87c086`; bench file has **250** tasks
- `make test` → **13** tests OK; simulator healthy on `:8701`
- `make demo` baseline ≈ **36.0%** success, **0** errors; fault run ≈ **22.5%**, **~85** infra errors
- Escalation/refuse strong; act ≈ **0/60** (no tools yet) — expected on Day 01

# Relay targets. Replay mode is the default: no API key, no network.
SHELL := /bin/bash
PY := $(shell [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)
export PYTHONPATH := src:.
SIM ?= http://127.0.0.1:$(or $(NORTHWIND_PORT),8701)
PROVIDER ?= replay
AGENT ?= loop
FAULT ?= 500
RATE ?= 0.3

.PHONY: build run test test-day01 test-day02 verify demo score fault-on fault-off record clean

build:      ## seed the world and generate Relay-Bench
	$(PY) src/northwind/seed.py --db data/northwind.db
	$(PY) src/relaybench/tasks.py

run:        ## start everything
	./start.sh

test:       ## full offline test suite
	$(PY) -m unittest discover -s tests -t . -v

test-day01: ## Day 01 DONE WHEN: baseline score recorded on the scoreboard
	$(PY) -m unittest tests.test_day01 -v

test-day02: ## Day 02 DONE WHEN: same task passes live and in replay; caps tested
	$(PY) -m unittest tests.test_day02 -v

verify: test
	@curl -fs $(SIM)/health >/dev/null && echo "simulator healthy" || (echo "simulator down: run ./start.sh"; exit 1)

demo:       ## baseline, agent loop, then the loop under injected faults
	./scripts/demo.sh

score:      ## show scoreboard history
	$(PY) src/relaybench/scoreboard.py

fault-on:
	@curl -fs -X POST $(SIM)/_faults -d '{"mode":"$(FAULT)","rate":$(RATE)}'; echo

fault-off:
	@curl -fs -X POST $(SIM)/_faults -d '{"mode":"off","rate":0}'; echo

record:     ## re-record replay fixtures (AGENT=loop|baseline, PROVIDER=stub|live)
	$(PY) src/relaybench/run.py --sim $(SIM) --agent $(AGENT) --provider record:$(PROVIDER) --label record-$(PROVIDER)

clean:
	./stop.sh --clean

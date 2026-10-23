# Relay targets. Replay mode is the default: no API key, no network.
SHELL := /bin/bash
PY := $(shell [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)
export PYTHONPATH := src:.
SIM ?= http://127.0.0.1:$(or $(NORTHWIND_PORT),8701)
PROVIDER ?= replay
AGENT ?= typed
FAULT ?= 500
RATE ?= 0.3

.PHONY: build run test test-day01 test-day02 test-day03 verify demo bench score fault-on fault-off record clean

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

test-day03: ## Day 03 DONE WHEN: injected bad-argument faults are recovered in tests
	$(PY) -m unittest tests.test_day03 -v

verify: test
	@curl -fs $(SIM)/health >/dev/null && echo "simulator healthy" || (echo "simulator down: run ./start.sh"; exit 1)

demo:       ## baseline, loop, typed tools, then typed tools under injected faults
	./scripts/demo.sh

bench:      ## one Relay-Bench run (AGENT=baseline|loop|typed, LABEL=name); honours fault-on
	$(PY) src/relaybench/run.py --sim $(SIM) --agent $(AGENT) $(if $(LABEL),--label $(LABEL),)

score:      ## show scoreboard history
	$(PY) src/relaybench/scoreboard.py

fault-on:   ## FAULT=timeout|429|500|malformed|lost break the world; FAULT=bad_args breaks the model's arguments
	@if [ "$(FAULT)" = "bad_args" ]; then mkdir -p .run && echo "mixed:$(RATE)" > .run/arg-fault && echo "bad_args at rate $(RATE)"; \
	else curl -fs -X POST $(SIM)/_faults -d '{"mode":"$(FAULT)","rate":$(RATE)}'; echo; fi

fault-off:  ## clear both kinds of fault
	@rm -f .run/arg-fault
	@curl -fs -X POST $(SIM)/_faults -d '{"mode":"off","rate":0}'; echo

record:     ## re-record replay fixtures (AGENT=typed|loop|baseline, PROVIDER=stub|live)
ifeq ($(AGENT),typed)
	$(PY) src/relaybench/run.py --sim $(SIM) --agent typed --provider record:$(PROVIDER) --arg-fault off --label record-typed-clean
	RELAY_RECORD_APPEND=1 $(PY) src/relaybench/run.py --sim $(SIM) --agent typed --provider record:$(PROVIDER) --arg-fault mixed:1.0 --label record-typed-faulted
else
	$(PY) src/relaybench/run.py --sim $(SIM) --agent $(AGENT) --provider record:$(PROVIDER) --label record-$(PROVIDER)
endif

clean:
	./stop.sh --clean

.PHONY: dev setup lint typecheck test test-integration up down health langfuse-check keys-check trace-smoke eval calib-export calibrate canary-check data ingest search chat ci

setup:            ## install Python + web deps, create .env
	uv sync
	cd web && npm ci
	test -f .env || cp .env.example .env

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run mypy
	cd web && npm run typecheck

test:
	uv run pytest -q

test-integration:   ## needs `make up` first
	uv run pytest -q -m integration

up:
	docker compose up -d --build --wait

dev: up       ## spec §11 alias: full stack up, then health
	$(MAKE) health

down:
	docker compose down

health:
	curl -fsS localhost:8000/health && echo && curl -fsS -o /dev/null -w "web %{http_code}\n" localhost:3000

langfuse-check:
	PYTHONPATH=. uv run python scripts/check_langfuse.py

keys-check:       ## OpenAI + Anthropic keys: where they come from and whether they work
	PYTHONPATH=. uv run python scripts/check_keys.py

trace-smoke:      ## one mock-model run traced to Langfuse; needs `make up`
	PYTHONPATH=. uv run python scripts/trace_smoke.py

eval:             ## make eval AGENT=<name> [ARGS="--smoke --repeat 3 --max-usd 5"]
	PYTHONPATH=. uv run python -m core.evals $(AGENT) $(ARGS)

calib-export:     ## pick 30 judged answers to hand-label: make calib-export RESULTS=<run>.json
	PYTHONPATH=. uv run python scripts/judge_calibration.py export $(RESULTS)

canary-check:     ## SHR-019: planted PII canaries absent from >=100 exported eval traces
	PYTHONPATH=. uv run python scripts/check_canaries.py

calibrate:        ## re-judge the hand-labelled answers and report agreement (target 85%)
	PYTHONPATH=. uv run python scripts/judge_calibration.py

data:             ## rebuild eval sets (cached generations); ARGS=--refresh re-generates (OpenAI key)
	PYTHONPATH=. uv run python -m data.generators.support_evals $(ARGS)

ingest:           ## (re)index the support KB; ARGS="--embedder hash" works without a key
	PYTHONPATH=. uv run python -m core.retrieval ingest $(ARGS)

search:           ## make search Q="check-out time"
	PYTHONPATH=. uv run python -m core.retrieval search -q "$(Q)" $(ARGS)

chat:             ## talk to the Concierge; ID=<conversation> resumes one
	PYTHONPATH=. uv run python -m agents.concierge $(ID)

ci: lint typecheck test

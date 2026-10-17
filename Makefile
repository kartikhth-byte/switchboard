.PHONY: dev setup lint typecheck test test-integration up down health langfuse-check trace-smoke eval ci

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

trace-smoke:      ## one mock-model run traced to Langfuse; needs `make up`
	PYTHONPATH=. uv run python scripts/trace_smoke.py

eval:             ## make eval AGENT=<name> [ARGS="--smoke --repeat 3 --max-usd 5"]
	PYTHONPATH=. uv run python -m core.evals $(AGENT) $(ARGS)

ci: lint typecheck test

.PHONY: dev setup lint typecheck test test-integration up down health langfuse-check keys-check trace-smoke eval calib-export calibrate canary-check lead-burst integrations-check data data-lead ingest search chat ci

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

secrets-scan:     ## LEAD-016/SHR-005: no keys in Langfuse traces (7 days) or api/worker logs
	docker compose logs --no-color api worker 2>/dev/null | PYTHONPATH=. uv run python scripts/check_secrets.py --logs -

n8n-import:       ## LEAD-007: start n8n, import + activate n8n/lead_intake.json
	docker compose --profile n8n up -d n8n
	sleep 8
	docker compose exec -T n8n n8n import:workflow --separate --input=/workflows/
	docker compose exec -T n8n n8n update:workflow --id=SwbLeadIntake001 --active=true
	docker compose restart n8n
	@echo "n8n UI: http://localhost:5678  webhook: http://localhost:5678/webhook/lead-intake"

n8n-e2e:          ## LEAD-007: form fixture -> n8n webhook -> agent API (needs `make up` + n8n-import)
	PYTHONPATH=. uv run python scripts/n8n_e2e.py

call:             ## M4: talk to the Operator (salon receptionist) by typing
	PYTHONPATH=. uv run python -m agents.operator $(ARGS)

integrations-check: ## M3.3: one live call each to Slack, WhatsApp, GoHighLevel, Mailpit
	PYTHONPATH=. uv run python scripts/integrations_check.py

lead-burst:       ## LEAD-018: 30 leads at once, p95 intake -> reply queued <=60s (needs `make up`)
	PYTHONPATH=. uv run python scripts/lead_burst.py $(ARGS)

calibrate:        ## re-judge the hand-labelled answers and report agreement (target 85%)
	PYTHONPATH=. uv run python scripts/judge_calibration.py

data:             ## rebuild eval sets (cached generations); ARGS=--refresh re-generates (OpenAI key)
	PYTHONPATH=. uv run python -m data.generators.support_evals $(ARGS)
	PYTHONPATH=. uv run python -m data.generators.lead_evals $(ARGS)

data-lead:        ## rebuild Dispatcher eval sets only; ARGS=--refresh re-paraphrases (OpenAI key)
	PYTHONPATH=. uv run python -m data.generators.lead_evals $(ARGS)

ingest:           ## (re)index the support KB; ARGS="--embedder hash" works without a key
	PYTHONPATH=. uv run python -m core.retrieval ingest $(ARGS)

search:           ## make search Q="check-out time"
	PYTHONPATH=. uv run python -m core.retrieval search -q "$(Q)" $(ARGS)

chat:             ## talk to the Concierge; ID=<conversation> resumes one
	PYTHONPATH=. uv run python -m agents.concierge $(ID)

ci: lint typecheck test

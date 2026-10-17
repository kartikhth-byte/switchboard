# Switchboard

A portfolio of business AI agents — **Concierge** (support), **Dispatcher** (lead qualification),
**Operator** (voice receptionist) — on a shared Python core. Built from real Upwork postings.

> Status: **M0 — repo + infra.** Agents land in M2–M4.

## Quickstart

```bash
cp .env.example .env
docker compose up -d --build --wait     # postgres(+pgvector), api :8000, web :3000
make health
```

Optional services:

```bash
docker compose --profile n8n up -d        # n8n :5678 (Dispatcher intake, M3)
docker compose --profile langfuse up -d   # self-hosted Langfuse :3001 (fallback; Cloud is default)
```

Local dev without Docker for the app code: `make setup`, then `make ci` (ruff, mypy, pytest).
Check Langfuse Cloud keys in `.env` with `make langfuse-check`.

## Safety defaults

- `DEMO_INTEGRATIONS=mock` — every external integration is mocked unless set to `real`.
- `OUTBOUND_MODE=off` — no real outbound messages unless an allowlist is configured.
- Secrets only from env; `.env` is git-ignored. No real personal data in the repo.

## Layout

```
core/          shared: models, tools, runs, approvals, guardrails, redaction, retrieval, memory, config, evals
integrations/  adapters (mock + real): crm, messaging, slack, calendar, email, storage, sourcing
agents/        concierge, dispatcher, operator
api/           FastAPI app
web/           Next.js demo app
data/          synthetic generators, fixtures, cards
n8n/           exported workflows
deploy/        Dockerfiles, DB init
```

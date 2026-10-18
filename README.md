# Switchboard

A portfolio of business AI agents — **Concierge** (support), **Dispatcher** (lead qualification),
**Operator** (voice receptionist) — on a shared Python core. Built from real Upwork postings.

> Status: **M2 — Concierge built.** Results: [`evals/results/concierge/summary.md`](evals/results/concierge/summary.md).
> Dispatcher (M3) and Operator (M4) next.

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

## Concierge (support agent)

```bash
make up && make ingest          # stack + index the 25-doc Palmgrove Stays KB (needs OPENAI_API_KEY)
open http://localhost:3000/support   # demo page: citations, trace link, escalation banner, cost
make chat                       # same agent in the terminal; ID=<conversation> resumes one
make eval AGENT=concierge ARGS="--smoke --max-usd 3"   # ~20 dev cases, ~$1
```

Test booking: `PG-1002` / `rohan.fernandes@example.com`. Everything outbound is mocked.

## Debugging a run

Every guest message is one **run** with a `run_id`; the same id ties together the log lines,
the Langfuse trace and the `runs` row.

1. **Find the run.** The demo page and `make chat` print `run_id`, status, tools used and a
   "view trace" link. The trace id is derived from the run id (`core.tracing.trace_id_for`).
2. **Read the trace** (Langfuse): one span per loop step and per tool call, with tool errors as
   span status. Inputs and outputs are PII-masked before export.
3. **Read the logs.** JSON lines on stdout, each with `run_id` and `agent`:
   `docker compose logs api | grep <run_id>`.
4. **Check the row.** `SELECT status, error, tokens_in, tokens_out, cost_usd FROM runs WHERE id = '<run_id>';`
   Status is one of `done`, `waiting_approval`, `budget_exceeded`, `failed`; a handoff is a
   `done` run whose trace has a `handoff_to_human` span.
5. **Inspect the conversation.** `make chat ID=<conversation_id>`, then `/state` shows the
   verified identity, remembered facts, summary coverage and any pending approval.

| Symptom | Likely cause | Fix |
|---|---|---|
| "I've passed this to a member of our team" with no answer | step limit, repeated tool error, or `requires_human` from a tool | open the trace; the last tool span has the error |
| Reply mentions a budget or daily limit | `DAILY_USD_CAP` / per-run token budget reached | raise the cap in `.env`; evals ignore the daily cap |
| `invalid_citations` in the trace | model cited a chunk it didn't retrieve this turn | the loop asks it to retry; repeated -> handoff |
| Empty or off-topic answers | KB not indexed, or indexed with another embedder | `make ingest`, check `make search Q="..."` |
| 429 from `/concierge/chat` | per-IP rate limit (`RATE_LIMIT_PER_MIN`) | wait, or raise it locally |

Eval failures: each results JSON keeps the transcript, tool calls, retrieved chunks and judge
reasons per case (`evals/results/<agent>/<date>.json`). `make calibrate` re-checks the judge
against the hand labels; `make canary-check` scans exported traces for planted PII.

## Production notes (what this demo doesn't do)

Escalation is the part a real deployment would harden first. The demo classifies every guest
message against configured triggers and hands off in code when the agent didn't
(`agents/concierge/prompts/escalation.md`). A production version would add:

- **More than one signal:** a catch-all "needs a person" category, keyword rules for critical
  words (police, injured, fire) that don't depend on a model, and loop detection.
- **Per-category thresholds:** safety and emergency escalate even when unsure; "upset" and
  exception requests need more certainty, or first *offer* a person.
- **An always-visible "talk to a person" button**, so the guest has an exit when every check misses.
- **Pause the bot after handoff** until a person hands the conversation back.
- **Find the misses:** log every decision, review a weekly sample of non-escalated chats, watch
  thumbs-down, bad reviews and guests who contact you on another channel; staff mark
  unnecessary handoffs. Every real miss becomes an eval case.
- **Shadow mode** for any new classifier or category before it goes live.
- **Operations:** alerts when a handoff isn't picked up, on-call cover for safety cases.

Ideas logged for later are in [`docs/phase2_backlog.md`](docs/phase2_backlog.md).

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

# n8n: lead intake workflow (LEAD-007)

`lead_intake.json`: **Form webhook → Map to lead schema → POST /agents/lead/intake → Reply to form**.

```bash
make up            # API, worker, Postgres
make n8n-import    # start n8n (profile), import + activate the workflow
make n8n-e2e       # post the form fixture through n8n; check create, replay and processing
```

Webhook: `POST http://localhost:5678/webhook/lead-intake` with form fields `full_name, email,
phone, message, utm_source, utm_medium, utm_campaign`. No credentials are needed: the workflow
calls the API on the compose network (`http://api:8000`).

**Variance from the spec sketch** (Webhook → API → IF hot → Slack): n8n does intake only. Whether a
lead is hot is decided after the model assessment, which runs asynchronously in the worker, so the
Dispatcher posts the Slack alert itself (routing rules in `agents/dispatcher/config/agent.yaml`).
Keeping routing in one place avoids double alerts and keeps the rule testable in code.

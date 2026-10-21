"""LEAD-007 end-to-end: the form fixture goes through the imported n8n workflow into the agent API.

    make n8n-import   # once: start n8n, import n8n/lead_intake.json, activate it
    make n8n-e2e

Posts data/fixtures/lead/n8n_form.json (with a fresh +tag on the email) to the n8n webhook,
checks a lead was created, replays it (must return the same lead, created=false), then waits
for the worker to finish the lead.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
WEBHOOK = "http://localhost:5678/webhook/lead-intake"
API = "http://localhost:8000"


def main() -> int:
    form = json.loads((ROOT / "data/fixtures/lead/n8n_form.json").read_text())
    user, domain = form["email"].split("@")
    form["email"] = f"{user}+n8n{int(time.time())}@{domain}"
    with httpx.Client(timeout=30) as http:
        for _ in range(60):  # n8n takes a few seconds after `make n8n-import` restarts it
            try:
                if http.get("http://localhost:5678/healthz").status_code == 200:
                    break
            except httpx.TransportError:
                pass
            time.sleep(1)
        else:
            print("n8n is not up on :5678 (docker compose --profile n8n up -d n8n)")
            return 1
        time.sleep(2)  # webhooks register just after the health endpoint answers
        r = http.post(WEBHOOK, json=form)
        if r.status_code == 404:
            print("n8n webhook not found: run `make n8n-import` first")
            return 1
        first = r.json()
        again = http.post(WEBHOOK, json=form).json()
        print("first delivery:", first, "| replay:", again)
        lead_id = first.get("lead_id")
        ok = bool(lead_id) and first.get("created") is True
        ok &= again.get("lead_id") == lead_id and again.get("created") is False
        stage = None
        for _ in range(60):
            lead = http.get(f"{API}/agents/lead/{lead_id}").json()["lead"]
            stage = lead["stage"]
            if stage not in ("normalized", "processing", "in_crm", "assessed", "drafted"):
                break
            time.sleep(1)
        print(
            f"lead {lead_id}: stage={stage} tier={(lead.get('assessment') or {}).get('tier')}"
            f" crm_id={lead.get('crm_id')} contact={lead.get('contact')}"
        )
    ok &= stage in ("with_owner", "nurturing", "held", "contacted", "awaiting_approval")
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

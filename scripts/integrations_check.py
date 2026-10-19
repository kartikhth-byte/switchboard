"""Live check of the real integrations, one call each (M3.3). Run on your machine with
DEMO_INTEGRATIONS=real and the keys in .env:

    make integrations-check

Sends only to recipients on OUTBOUND_ALLOWLIST: a Slack message to #ops-alerts, Meta's
hello_world WhatsApp template to the first allowlisted phone, one GoHighLevel test contact
(an example.org address), and one email into Mailpit (http://localhost:8025).
"""

from __future__ import annotations

import sys

from agents.dispatcher.service import make_adapters
from core.config.settings import allowlist, get_settings


def main() -> int:
    s = get_settings()
    if s.demo_integrations != "real":
        print("set DEMO_INTEGRATIONS=real in .env first")
        return 1
    ad = make_adapters(s)  # raises with the missing env var names
    allowed = allowlist(s)
    phone = next((a for a in sorted(allowed) if a.startswith("+") and not a.endswith("*")), None)
    checks = {
        "slack": lambda: ad.slack.post("#ops-alerts", "Switchboard integrations check ✅"),
        "ghl": lambda: ad.crm.upsert_lead(
            "integrations-check@example.org",
            None,
            {"name": "Integrations Check", "source": "check"},
        ),
        "email": lambda: ad.email.send(
            "integrations-check@example.org", "Switchboard check", "Mailpit works."
        ),
    }
    if phone:
        from integrations.messaging.real import WhatsAppCloud

        wa = ad.whatsapp
        if isinstance(wa, WhatsAppCloud):
            wa.language = "en_US"  # Meta's sample template exists only in en_US
        checks["whatsapp"] = lambda: wa.send_template(phone, "hello_world", [])
    else:
        print("whatsapp: skipped (no exact +phone on OUTBOUND_ALLOWLIST)")
    failed = 0
    for name, fn in checks.items():
        try:
            print(f"{name}: ok ({fn()})")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"{name}: FAILED {type(exc).__name__}: {str(exc)[:200]}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

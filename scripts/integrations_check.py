"""Live check of the real integrations, one call each (M3.3):

    make integrations-check

Checks every service whose keys are in .env and skips the rest, so you can set them up one at
a time. Sends only to recipients on OUTBOUND_ALLOWLIST: a Slack message to #ops-alerts, Meta's
hello_world WhatsApp template to the first allowlisted phone, one GoHighLevel test contact
(an example.org address), and one email into Mailpit (http://localhost:8025).
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

from core.config.settings import allowlist, get_settings, missing_real_settings


def main() -> int:
    s = get_settings()
    checks: dict[str, Callable[[], Any]] = {}
    skipped: dict[str, str] = {}

    if missing := missing_real_settings(s, ("slack",)):
        skipped["slack"] = ", ".join(missing)
    else:
        from integrations.slack.real import SlackWeb, parse_channels

        assert s.slack_bot_token
        slack = SlackWeb(s.slack_bot_token.get_secret_value(), parse_channels(s.slack_channels))
        checks["slack"] = lambda: slack.post("#ops-alerts", "Switchboard integrations check ✅")

    if missing := missing_real_settings(s, ("whatsapp",)):
        skipped["whatsapp"] = ", ".join(missing)
    else:
        from integrations.messaging.real import WhatsAppCloud

        allowed = allowlist(s)
        phone = next((a for a in sorted(allowed) if a.startswith("+") and "*" not in a), None)
        if phone is None:
            skipped["whatsapp"] = "an exact +phone on OUTBOUND_ALLOWLIST"
        else:
            assert s.whatsapp_token
            wa = WhatsAppCloud(
                s.whatsapp_phone_number_id,
                s.whatsapp_token.get_secret_value(),
                s.whatsapp_api_version,
                "en_US",
            )  # hello_world exists in en_US only
            checks["whatsapp"] = lambda: wa.send_template(phone, "hello_world", [])

    if missing := missing_real_settings(s, ("ghl",)):
        skipped["ghl"] = ", ".join(missing)
    else:
        from integrations.crm.ghl import GoHighLevelCRM

        assert s.ghl_token
        crm = GoHighLevelCRM(s.ghl_token.get_secret_value(), s.ghl_location_id)
        checks["ghl"] = lambda: crm.upsert_lead(
            "integrations-check@example.org",
            None,
            {"name": "Integrations Check", "source": "check"},
        )

    from integrations.messaging.real import SmtpEmail

    mail = SmtpEmail(s.smtp_host, s.smtp_port, s.email_from)
    checks["email"] = lambda: mail.send(
        "integrations-check@example.org", "Switchboard check", "Mailpit works."
    )

    failed = 0
    for name, fn in checks.items():
        try:
            print(f"{name}: ok ({fn()})")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"{name}: FAILED {type(exc).__name__}: {str(exc)[:200]}")
    for name, why in skipped.items():
        print(f"{name}: skipped (needs {why})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

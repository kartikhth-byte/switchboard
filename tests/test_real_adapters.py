"""Real adapters against fake HTTP/SMTP: request shape, auth headers, error handling.
No network, no tokens (the live check is `make integrations-check` with DEMO_INTEGRATIONS=real)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from core.config.settings import Settings, missing_real_settings
from integrations.crm.ghl import GoHighLevelCRM
from integrations.http import IntegrationError
from integrations.messaging.real import SmtpEmail, WhatsAppCloud
from integrations.slack import MockSlack, make_slack
from integrations.slack.real import SlackWeb, parse_channels


def recorder(response: dict[str, Any], status: int = 200) -> tuple[list[httpx.Request], Any]:
    seen: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(status, json=response)

    return seen, httpx.MockTransport(handle)


def test_slack_posts_to_mapped_channel_with_blocks() -> None:
    seen, t = recorder({"ok": True, "ts": "1712.0001"})
    s = SlackWeb(
        "xoxb-test", parse_channels("#leads=C111, #ops-alerts=C222"), httpx.Client(transport=t)
    )
    ts = s.post(
        "#leads",
        "Hot lead",
        {"owner": "@reservations", "tier": 1, "thread": [{"from": "lead", "text": "6 of us"}]},
    )
    body = json.loads(seen[0].content)
    assert ts == "1712.0001" and body["channel"] == "C111"
    assert seen[0].headers["authorization"] == "Bearer xoxb-test"
    assert any("@reservations" in json.dumps(b) for b in body["blocks"])


def test_slack_ok_false_raises_without_leaking_token() -> None:
    _, t = recorder({"ok": False, "error": "not_in_channel"})
    with pytest.raises(IntegrationError) as e:
        SlackWeb("xoxb-secret", {}, httpx.Client(transport=t)).post("#leads", "x")
    assert "not_in_channel" in str(e.value) and "xoxb-secret" not in str(e.value)


def test_whatsapp_text_and_template_bodies() -> None:
    seen, t = recorder({"messages": [{"id": "wamid.1"}]})
    wa = WhatsAppCloud("12345", "tok", "v21.0", "en", httpx.Client(transport=t))
    assert wa.send_text("+919876543210", "Hi Asha") == "wamid.1"
    wa.send_template("+919876543210", "palmgrove_enquiry_ack", ["Asha"])
    wa.send_template("+919876543210", "hello_world", [])
    text, tpl, bare = (json.loads(r.content) for r in seen)
    assert str(seen[0].url) == "https://graph.facebook.com/v21.0/12345/messages"
    assert text["to"] == "919876543210" and text["text"]["body"] == "Hi Asha"
    assert tpl["template"]["components"][0]["parameters"] == [{"type": "text", "text": "Asha"}]
    assert bare["template"]["components"] == []


def test_whatsapp_http_error_raises() -> None:
    _, t = recorder({"error": {"message": "Recipient not in allowed list"}}, status=400)
    with pytest.raises(IntegrationError, match="HTTP 400"):
        WhatsAppCloud("1", "tok", client=httpx.Client(transport=t)).send_text("+1", "x")


def test_ghl_upsert_body_and_headers() -> None:
    seen, t = recorder({"new": True, "contact": {"id": "c-42"}})
    crm = GoHighLevelCRM(
        "pit-tok",
        "loc-1",
        httpx.Client(base_url="https://services.leadconnectorhq.com", transport=t),
    )
    cid = crm.upsert_lead(
        "asha@example.org",
        "+919876543210",
        {
            "name": "Asha Rao",
            "channel": "web_form",
            "campaign": "festive_2026",
            "source": "website",
        },
    )
    body, req = json.loads(seen[0].content), seen[0]
    assert cid == "c-42" and req.url.path == "/contacts/upsert"
    assert (
        req.headers["version"] == "2021-07-28" and req.headers["authorization"] == "Bearer pit-tok"
    )
    assert body == {
        "locationId": "loc-1",
        "firstName": "Asha",
        "lastName": "Rao",
        "email": "asha@example.org",
        "phone": "+919876543210",
        "source": "website",
        "tags": ["switchboard", "web_form", "festive_2026"],
    }
    assert crm.contact_url("c-42").endswith("/location/loc-1/contacts/detail/c-42")


def test_smtp_email_sends_one_message(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[Any] = []

    class FakeSMTP:
        def __init__(self, host: str, port: int, timeout: int) -> None:
            assert (host, port) == ("mailpit", 1025)

        def __enter__(self) -> FakeSMTP:
            return self

        def __exit__(self, *a: Any) -> None:
            pass

        def send_message(self, msg: Any) -> None:
            sent.append(msg)

    monkeypatch.setattr("integrations.messaging.real.smtplib.SMTP", FakeSMTP)
    mid = SmtpEmail("mailpit", 1025, "Palmgrove <hello@palmgrove.test>").send(
        "asha@example.org", "Your enquiry", "Hi Asha"
    )
    assert mid.startswith("<") and sent[0]["To"] == "asha@example.org"


def test_real_mode_fails_fast_and_mock_by_default() -> None:
    real = Settings(_env_file=None, demo_integrations="real")  # type: ignore[call-arg]
    assert missing_real_settings(real, ("slack", "whatsapp", "ghl")) == [
        "SLACK_BOT_TOKEN",
        "WHATSAPP_PHONE_NUMBER_ID",
        "WHATSAPP_TOKEN",
        "GHL_TOKEN",
        "GHL_LOCATION_ID",
    ]
    from agents.dispatcher.service import make_adapters

    with pytest.raises(RuntimeError, match="SLACK_BOT_TOKEN"):
        make_adapters(real)
    assert isinstance(make_slack(Settings(_env_file=None)), MockSlack)  # type: ignore[call-arg]
    assert isinstance(make_slack(real), MockSlack)  # no token: Concierge stays on the mock

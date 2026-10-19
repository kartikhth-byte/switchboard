"""Real outbound adapters for demo runs: WhatsApp Cloud API (Meta test number) and SMTP email
(Mailpit locally, so nothing leaves the machine). They only deliver; the Dispatcher's tools
decide whether a message may go."""

from __future__ import annotations

import smtplib
import uuid
from email.message import EmailMessage

import httpx

from integrations.http import TIMEOUT, check


class WhatsAppCloud:
    def __init__(
        self,
        phone_number_id: str,
        token: str,
        api_version: str = "v21.0",
        language: str = "en",
        client: httpx.Client | None = None,
    ) -> None:
        self.url = f"https://graph.facebook.com/{api_version}/{phone_number_id}/messages"
        self._headers = {"Authorization": f"Bearer {token}"}
        self.language = language
        self.http = client or httpx.Client(timeout=TIMEOUT)

    def _send(self, body: dict) -> str:  # type: ignore[type-arg]
        body = {"messaging_product": "whatsapp", **body}
        data = check(self.http.post(self.url, headers=self._headers, json=body), "whatsapp")
        return str(data["messages"][0]["id"])

    def send_text(self, to: str, body: str) -> str:
        return self._send(
            {"to": to.lstrip("+"), "type": "text", "text": {"preview_url": False, "body": body}}
        )

    def send_template(self, to: str, template: str, params: list[str]) -> str:
        components = (
            [{"type": "body", "parameters": [{"type": "text", "text": p} for p in params]}]
            if params
            else []
        )
        return self._send(
            {
                "to": to.lstrip("+"),
                "type": "template",
                "template": {
                    "name": template,
                    "language": {"code": self.language},
                    "components": components,
                },
            }
        )


class SmtpEmail:
    """Mailpit in compose (SMTP :1025, web UI :8025). No auth, no TLS: local demo only."""

    def __init__(self, host: str, port: int, sender: str) -> None:
        self.host, self.port, self.sender = host, port, sender

    def send(self, to: str, subject: str, body: str) -> str:
        msg = EmailMessage()
        mid = f"<{uuid.uuid4().hex}@switchboard.local>"
        msg["From"], msg["To"], msg["Subject"], msg["Message-ID"] = self.sender, to, subject, mid
        msg.set_content(body)
        with smtplib.SMTP(self.host, self.port, timeout=10) as s:
            s.send_message(msg)
        return mid

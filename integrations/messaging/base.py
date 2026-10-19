"""Outbound messaging interfaces (LEAD-003). Adapters only deliver: the Dispatcher's send tools
decide whether a message may go (outbound mode, allowlist, opt-out, WhatsApp 24h window) and
record every attempt in the `outbox` table."""

from __future__ import annotations

from typing import Any, Protocol


class WhatsApp(Protocol):
    def send_text(self, to: str, body: str) -> str: ...  # -> provider message id
    def send_template(self, to: str, template: str, params: list[str]) -> str: ...


class Email(Protocol):
    def send(self, to: str, subject: str, body: str) -> str: ...


class LeadCRM(Protocol):
    def upsert_lead(self, email: str | None, phone: str | None, fields: dict[str, Any]) -> str:
        """Create or update the contact for this email/phone; returns the CRM contact id."""
        ...

    def contact_url(self, crm_id: str) -> str: ...

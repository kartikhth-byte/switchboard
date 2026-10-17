"""In-memory CRM for tests and the demo (CI default). Records every write for assertions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class MockCRM:
    contacts: dict[str, dict[str, Any]] = field(default_factory=dict)
    tickets: list[dict[str, Any]] = field(default_factory=list)
    writes: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def get_contact(self, email: str) -> dict[str, Any] | None:
        return self.contacts.get(email.lower())

    def upsert_contact(self, email: str, fields: dict[str, Any]) -> dict[str, Any]:
        contact = self.contacts.setdefault(email.lower(), {"email": email.lower()})
        contact.update(fields)
        self.writes.append(("upsert_contact", {"email": email.lower(), **fields}))
        return dict(contact)

    def create_ticket(
        self, email: str, subject: str, body: str, category: str, priority: str
    ) -> dict[str, Any]:
        ticket = {
            "ticket_id": f"T-{len(self.tickets) + 1:04d}",
            "email": email.lower(),
            "subject": subject,
            "body": body,
            "category": category,
            "priority": priority,
            "status": "open",
        }
        self.tickets.append(ticket)
        self.writes.append(("create_ticket", ticket))
        return dict(ticket)

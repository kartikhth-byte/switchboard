"""CI/default adapters: deliver nothing, return fake ids (the outbox table is the record)."""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import count
from typing import Any


@dataclass
class MockWhatsApp:
    sent: list[dict[str, Any]] = field(default_factory=list)
    _ids: Any = field(default_factory=lambda: count(1))

    def send_text(self, to: str, body: str) -> str:
        self.sent.append({"to": to, "body": body})
        return f"wamid.mock-{next(self._ids)}"

    def send_template(self, to: str, template: str, params: list[str]) -> str:
        self.sent.append({"to": to, "template": template, "params": params})
        return f"wamid.mock-{next(self._ids)}"


@dataclass
class MockEmail:
    sent: list[dict[str, Any]] = field(default_factory=list)

    def send(self, to: str, subject: str, body: str) -> str:
        self.sent.append({"to": to, "subject": subject, "body": body})
        return f"mock-email-{len(self.sent)}"

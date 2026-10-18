"""CRM connector interface (SHR-012): contacts + tickets now; leads arrive in M3."""

from __future__ import annotations

from typing import Any, Protocol


class ContactExists(Exception):
    """create_contact on an email that is already a contact."""


class CRM(Protocol):
    def get_contact(self, email: str) -> dict[str, Any] | None: ...
    def upsert_contact(self, email: str, fields: dict[str, Any]) -> dict[str, Any]: ...
    def create_contact(self, email: str, fields: dict[str, Any]) -> dict[str, Any]:
        """Create only; raises ContactExists if the email is already a contact."""
        ...

    def create_ticket(
        self, email: str, subject: str, body: str, category: str, priority: str
    ) -> dict[str, Any]: ...

"""Postgres-backed mock CRM for the Dispatcher (CI default, spec §6.2). Upserts by email, then
phone, so a replayed or cross-channel enquiry lands on one contact (LEAD-005)."""

from __future__ import annotations

import json
import uuid
from typing import Any

from core.db import connect


class PgMockCRM:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def upsert_lead(self, email: str | None, phone: str | None, fields: dict[str, Any]) -> str:
        with connect(self.dsn) as c:
            row = c.execute(
                "SELECT id, fields FROM crm_contacts"
                " WHERE (email = %s::text AND %s::text IS NOT NULL)"
                " OR (phone = %s::text AND %s::text IS NOT NULL) ORDER BY created_at LIMIT 1"
                " FOR UPDATE",
                (email, email, phone, phone),
            ).fetchone()
            if row is None:
                cid = f"mock-{uuid.uuid4().hex[:10]}"
                c.execute(
                    "INSERT INTO crm_contacts (id, email, phone, fields) VALUES (%s, %s, %s, %s)",
                    (cid, email, phone, json.dumps(fields)),
                )
                return cid
            merged = {
                **dict(row["fields"]),  # type: ignore[call-overload]
                **{k: v for k, v in fields.items() if v is not None},
            }
            c.execute(
                "UPDATE crm_contacts SET fields = %s, email = COALESCE(email, %s),"
                " phone = COALESCE(phone, %s), updated_at = now() WHERE id = %s",
                (json.dumps(merged), email, phone, row["id"]),
            )
            return str(row["id"])

    def contact_url(self, crm_id: str) -> str:
        return f"http://localhost:3000/lead?crm={crm_id}"

    def count(self) -> int:
        with connect(self.dsn) as c:
            row = c.execute("SELECT count(*) AS n FROM crm_contacts").fetchone()
        return int(row["n"]) if row else 0  # type: ignore[call-overload]

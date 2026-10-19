"""Lead records, events and the outbox (Postgres). Stage changes go through the state machine."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from agents.dispatcher.states import next_stage
from core.db import connect

UPDATABLE = {
    "contact",
    "crm_id",
    "assessment",
    "draft",
    "approval_id",
    "owner",
    "opted_out",
    "human_owner",
    "last_inbound_at",
    "run_id",
}
JSON_FIELDS = {"contact", "assessment", "draft"}


class Lead(BaseModel):
    id: UUID
    dedupe_key: str | None
    stage: str
    source: str | None
    payload: dict[str, Any]
    contact: dict[str, Any] | None = None
    crm_id: str | None = None
    assessment: dict[str, Any] | None = None
    draft: dict[str, Any] | None = None
    approval_id: UUID | None = None
    owner: str | None = None
    opted_out: bool = False
    human_owner: bool = False
    last_inbound_at: datetime | None = None
    run_id: UUID | None = None
    received_at: datetime | None = None
    created_at: datetime

    @property
    def stopped(self) -> bool:
        """No automated message may go to this lead (LEAD-013, LEAD-020)."""
        return (
            self.opted_out
            or self.human_owner
            or self.stage in {"opted_out", "replied", "with_owner", "closed", "merged", "failed"}
        )


class LeadStore:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def intake(
        self, payload: dict[str, Any], contact: dict[str, Any], dedupe_key: str | None
    ) -> tuple[Lead, bool]:
        """-> (lead, created). A replay or a second channel with the same email/phone returns the
        existing lead (LEAD-005); the delivery is logged as an event either way."""
        with connect(self.dsn) as c:
            row = c.execute(
                "INSERT INTO leads (dedupe_key, source, payload, contact, received_at, stage)"
                " VALUES (%s, %s, %s, %s, %s, 'normalized')"
                " ON CONFLICT (dedupe_key) DO NOTHING RETURNING *",
                (
                    dedupe_key,
                    payload.get("source"),
                    json.dumps(payload),
                    json.dumps(contact),
                    payload.get("received_at"),
                ),
            ).fetchone()
            created = row is not None
            if row is None:
                row = c.execute(
                    "SELECT * FROM leads WHERE dedupe_key = %s", (dedupe_key,)
                ).fetchone()
            assert row is not None
            c.execute(
                "INSERT INTO lead_events (lead_id, kind, data) VALUES (%s, %s, %s)",
                (
                    row["id"],
                    "delivery" if created else "duplicate_delivery",
                    json.dumps(
                        {"source": payload.get("source"), "channel": payload.get("channel")}
                    ),
                ),
            )
        return Lead.model_validate(row), created

    def get(self, lead_id: UUID | str) -> Lead | None:
        with connect(self.dsn) as c:
            row = c.execute("SELECT * FROM leads WHERE id = %s", (str(lead_id),)).fetchone()
        return Lead.model_validate(row) if row else None

    def advance(
        self, lead_id: UUID | str, event: str, data: dict[str, Any] | None = None, **fields: Any
    ) -> Lead:
        """Apply one state-machine event (raises InvalidTransition) and update fields atomically."""
        bad = set(fields) - UPDATABLE
        if bad:
            raise ValueError(f"not updatable: {bad}")
        with connect(self.dsn) as c:
            row = c.execute(
                "SELECT stage FROM leads WHERE id = %s FOR UPDATE", (str(lead_id),)
            ).fetchone()
            if row is None:
                raise KeyError(str(lead_id))
            stage = next_stage(str(row["stage"]), event)
            sets = ["stage = %s", "updated_at = now()"]
            vals: list[Any] = [stage]
            for k, v in fields.items():
                sets.append(f"{k} = %s")
                vals.append(json.dumps(v) if k in JSON_FIELDS and v is not None else v)
            updated = c.execute(
                f"UPDATE leads SET {', '.join(sets)} WHERE id = %s RETURNING *",  # noqa: S608
                (*vals, str(lead_id)),
            ).fetchone()
            c.execute(
                "INSERT INTO lead_events (lead_id, kind, data) VALUES (%s, %s, %s)",
                (str(lead_id), event, json.dumps({"stage": stage, **(data or {})})),
            )
        return Lead.model_validate(updated)

    def events(self, lead_id: UUID | str) -> list[dict[str, Any]]:
        with connect(self.dsn) as c:
            return list(
                c.execute(
                    "SELECT kind, data, at FROM lead_events WHERE lead_id = %s ORDER BY id",
                    (str(lead_id),),
                ).fetchall()
            )

    # ---- outbox: every send attempt, real or mock

    def record_message(
        self,
        lead_id: UUID | str | None,
        channel: str,
        recipient: str,
        body: str,
        status: str,
        *,
        template: str | None = None,
        provider_id: str | None = None,
        idem_key: str | None = None,
    ) -> bool:
        """-> False when this idem_key is already sending or sent (the send must not repeat).
        A key whose earlier attempt failed may be claimed again (retry)."""
        with connect(self.dsn) as c:
            row = c.execute(
                "INSERT INTO outbox (lead_id, channel, recipient, body, template, status,"
                " provider_id, idem_key) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT (idem_key) DO UPDATE SET status = EXCLUDED.status,"
                " created_at = now() WHERE outbox.status = 'failed' RETURNING id",
                (
                    str(lead_id) if lead_id else None,
                    channel,
                    recipient,
                    body,
                    template,
                    status,
                    provider_id,
                    idem_key,
                ),
            ).fetchone()
        return row is not None

    def mark_message(self, idem_key: str, status: str, provider_id: str | None = None) -> None:
        with connect(self.dsn) as c:
            c.execute(
                "UPDATE outbox SET status = %s, provider_id = COALESCE(%s, provider_id)"
                " WHERE idem_key = %s",
                (status, provider_id, idem_key),
            )

    def messages(self, lead_id: UUID | str, since: datetime | None = None) -> list[dict[str, Any]]:
        with connect(self.dsn) as c:
            return list(
                c.execute(
                    "SELECT channel, recipient, body, template, status, created_at FROM outbox"
                    " WHERE lead_id = %s AND (%s::timestamptz IS NULL OR created_at > %s)"
                    " ORDER BY id",
                    (str(lead_id), since, since),
                ).fetchall()
            )

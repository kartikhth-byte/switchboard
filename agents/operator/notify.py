"""Booking confirmations and reminders (VOICE-005).

On confirm: a `operator.confirm` job now, and an `operator.reminder` job at T-24h. On cancel
or reschedule, the old booking's jobs are cancelled by key prefix (`appt:<id>:`). The worker
sends through the shared WhatsApp/email adapters, under the same outbound rules as the
Dispatcher: OUTBOUND_MODE=off sends nothing, allowlist mode only to allowlisted recipients.
Every attempt is in `booking_audit` (sent / blocked / skipped), which also makes a retried
job idempotent.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from agents.operator.calendar import Calendar
from core.config.settings import Settings, allowlist
from core.db import connect
from core.guardrails.outbound import allowed
from core.jobs import JobQueue
from integrations.messaging import Email, WhatsApp

log = logging.getLogger(__name__)


@dataclass
class Notifier:
    cal: Calendar
    jobs: JobQueue

    def booked(self, booking_id: UUID | str, replaced: str | None = None) -> None:
        if replaced:
            self.cancelled(replaced)
        b = self.cal.get(booking_id)
        if not b:
            return
        bid = str(booking_id)
        self.jobs.enqueue("operator.confirm", {"booking_id": bid}, key=f"appt:{bid}:confirm")
        n = self.cal.salon.notifications
        at = b["start"] - timedelta(hours=n.reminder_hours_before)
        if at > self.cal.now():
            self.jobs.enqueue(
                "operator.reminder", {"booking_id": bid}, key=f"appt:{bid}:reminder", run_at=at
            )

    def cancelled(self, booking_id: UUID | str) -> int:
        return self.jobs.cancel_prefix(f"appt:{booking_id}:")


@dataclass
class Sender:
    """Worker side: turns a job into messages."""

    cal: Calendar
    settings: Settings
    whatsapp: WhatsApp
    email: Email

    def _done(self, booking_id: str, kind: str) -> bool:
        with connect(self.cal.dsn) as c:
            row = c.execute(
                "SELECT 1 FROM booking_audit WHERE appointment_id = %s AND action = %s",
                (booking_id, f"{kind}_sent"),
            ).fetchone()
        return row is not None

    def _log(self, booking_id: str, action: str, data: dict[str, Any]) -> None:
        with connect(self.cal.dsn) as c:
            c.execute(
                "INSERT INTO booking_audit (appointment_id, action, data) VALUES (%s, %s, %s)",
                (booking_id, action, json.dumps(data)),
            )

    def _gate(self, recipient: str) -> str | None:
        mode = self.settings.outbound_mode
        if mode == "off":
            return "outbound off"
        if not allowed(recipient, allowlist(self.settings)):
            return "not allowlisted"
        return None

    def send(self, booking_id: str, kind: str) -> str:
        """kind: confirmation | reminder. Returns what happened (for logs and tests)."""
        b = self.cal.get(booking_id)
        if b is None or b["status"] != "confirmed":
            self._log(booking_id, f"{kind}_skipped", {"reason": "not confirmed"})
            return "skipped"
        if self._done(booking_id, kind):
            return "already sent"
        salon = self.cal.salon
        svc = salon.service(b["service_id"]).name
        when = b["start"].astimezone(salon.tz)
        spoken = when.strftime("%A %d %B at %I:%M %p").replace(" 0", " ")
        name = (b["caller_name"] or "there").split()[0]
        who = salon.stylist(b["stylist_id"]).name
        lead = "Reminder: your" if kind == "reminder" else "Booked: your"
        body = f"{lead} {svc} with {who} at Studio Nine is on {spoken}. Reply or call to change it."
        results: dict[str, str] = {}
        targets = [("whatsapp", b["caller_phone"]), ("email", salon.notifications.front_desk_email)]
        for channel, to in targets:
            if not to:
                continue
            if (why := self._gate(to)) is not None:
                results[channel] = f"blocked: {why}"
                continue
            if channel == "whatsapp":  # first contact: an approved template, not free text
                pid = self.whatsapp.send_template(
                    to, salon.notifications.whatsapp_template, [name, svc, spoken]
                )
            else:
                pid = self.email.send(to, f"Studio Nine: {svc} {spoken} ({name})", body)
            results[channel] = f"sent {pid}"
        sent = any(v.startswith("sent") for v in results.values())
        self._log(booking_id, f"{kind}_sent" if sent else f"{kind}_blocked", results)
        return "sent" if sent else "blocked"

    def register_jobs(self, worker: Any) -> None:
        worker.register(
            "operator.confirm", lambda j: self.send(j.payload["booking_id"], "confirmation")
        )
        worker.register(
            "operator.reminder", lambda j: self.send(j.payload["booking_id"], "reminder")
        )

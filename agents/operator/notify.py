"""Booking confirmations and reminders (VOICE-005), and missed-call callbacks (VOICE-002).

Missed call: CallSession.end() decides in code (hung up silent, wanted a person nobody took,
dropped mid-booking) and enqueues `operator.callback`; the worker emails the front desk with
the number and the caller's last words (already redacted), gated like every other send.

Calendar mirror: every confirm/cancel/reschedule enqueues `operator.gcal_sync`, which copies the
row's current state to Google Calendar (demo) or the mock (CI).

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
from integrations.calendar import CalendarMirror
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
        self._sync(bid)
        self.jobs.enqueue("operator.confirm", {"booking_id": bid}, key=f"appt:{bid}:confirm")
        n = self.cal.salon.notifications
        at = b["start"] - timedelta(hours=n.reminder_hours_before)
        if at > self.cal.now():
            self.jobs.enqueue(
                "operator.reminder", {"booking_id": bid}, key=f"appt:{bid}:reminder", run_at=at
            )

    def cancelled(self, booking_id: UUID | str) -> int:
        n = self.jobs.cancel_prefix(f"appt:{booking_id}:")
        self._sync(str(booking_id))  # removes the mirrored event
        return n

    def missed(self, call_id: str, phone: str | None, reason: str, last_said: str) -> None:
        self.jobs.enqueue(
            "operator.callback",
            {"call_id": call_id, "phone": phone, "reason": reason, "last_said": last_said},
            key=f"call:{call_id}:callback",
        )

    def _sync(self, bid: str) -> None:
        """Mirror the booking's current state to Google Calendar (VOICE-003): the job reads
        the row when it runs, so it copies whatever Postgres says then."""
        self.jobs.enqueue("operator.gcal_sync", {"booking_id": bid}, key=f"gcal:{bid}")


@dataclass
class Sender:
    """Worker side: turns a job into messages."""

    cal: Calendar
    settings: Settings
    whatsapp: WhatsApp
    email: Email
    mirror: CalendarMirror | None = None

    def _done(self, booking_id: str, kind: str) -> bool:
        with connect(self.cal.dsn) as c:
            row = c.execute(
                "SELECT 1 FROM booking_audit WHERE appointment_id = %s AND action = %s",
                (booking_id, f"{kind}_sent"),
            ).fetchone()
        return row is not None

    def _log(
        self, booking_id: str | None, action: str, data: dict[str, Any], call_id: str | None = None
    ) -> None:
        with connect(self.cal.dsn) as c:
            c.execute(
                "INSERT INTO booking_audit (appointment_id, action, data, call_id)"
                " VALUES (%s, %s, %s, %s)",
                (booking_id, action, json.dumps(data), call_id),
            )

    def callback(self, p: dict[str, Any]) -> str:
        """Email the front desk about a missed call. Idempotent per call (a retried job)."""
        call_id = p["call_id"]
        with connect(self.cal.dsn) as c:
            done = c.execute(
                "SELECT 1 FROM booking_audit WHERE call_id = %s AND action = 'callback_sent'",
                (call_id,),
            ).fetchone()
        if done:
            return "already sent"
        to = self.cal.salon.notifications.front_desk_email
        number = p.get("phone") or "number withheld"
        body = (
            f"Missed call to Studio Nine: please call back {number}.\n"
            f"Why: the caller {p['reason']}.\n"
            f'Last thing they said: "{p.get("last_said") or "(nothing)"}"\n'
            f"Call id: {call_id}"
        )
        if (why := self._gate(to)) is not None:
            self._log(None, "callback_blocked", {"why": why, "reason": p["reason"]}, call_id)
            return "blocked"
        pid = self.email.send(to, f"Call back {number} (missed call)", body)
        self._log(None, "callback_sent", {"email": pid, "reason": p["reason"]}, call_id)
        return "sent"

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

    def sync_calendar(self, booking_id: str) -> str:
        b = self.cal.get(booking_id)
        if b is None or self.mirror is None:
            return "skipped"
        event_id = UUID(booking_id).hex
        if b["status"] != "confirmed":
            self.mirror.delete(event_id)
            self._log(booking_id, "gcal_deleted", {})
            return "deleted"
        salon = self.cal.salon
        svc, who = salon.service(b["service_id"]).name, salon.stylist(b["stylist_id"]).name
        first = (b["caller_name"] or "caller").split()[0]  # no phone numbers in the calendar
        self.mirror.upsert(
            event_id,
            f"{svc} — {first} ({who})",
            b["start"],
            b["end"],
            f"Booked by phone (Switchboard Operator). Booking {booking_id}.",
        )
        self._log(booking_id, "gcal_upserted", {"event_id": event_id})
        return "upserted"

    def register_jobs(self, worker: Any) -> None:
        worker.register("operator.gcal_sync", lambda j: self.sync_calendar(j.payload["booking_id"]))
        worker.register(
            "operator.confirm", lambda j: self.send(j.payload["booking_id"], "confirmation")
        )
        worker.register(
            "operator.reminder", lambda j: self.send(j.payload["booking_id"], "reminder")
        )
        worker.register("operator.callback", lambda j: self.callback(j.payload))

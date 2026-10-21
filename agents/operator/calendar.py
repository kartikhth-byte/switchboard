"""Postgres calendar of record (VOICE-003, VOICE-004).

Overlaps are impossible at the database level: holds and confirmed bookings share one table with
an exclusion constraint on (stylist, period). Two callers racing for the same slot both try to
insert a hold; exactly one succeeds. A hold lives `hold_ttl_seconds`; confirming re-checks it in
a transaction. Every action lands in `booking_audit`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.range import Range

from agents.operator.salon import Salon
from core.db import connect


class SlotTaken(Exception):
    """Someone else holds or booked an overlapping time for that stylist."""


class BookingError(Exception):
    """Unknown or expired hold/booking, a stylist without the skill, outside hours, ..."""


@dataclass(frozen=True)
class Slot:
    stylist_id: str
    service_id: str
    start: datetime
    end: datetime

    def as_dict(self, salon: Salon) -> dict[str, Any]:
        local = self.start.astimezone(salon.tz)
        return {
            "stylist_id": self.stylist_id,
            "stylist": salon.stylist(self.stylist_id).name,
            "service_id": self.service_id,
            "start": local.isoformat(),
            "spoken": local.strftime("%A %d %B, %I:%M %p").replace(" 0", " "),
        }


def _now() -> datetime:
    return datetime.now(UTC)


class Calendar:
    def __init__(self, dsn: str, salon: Salon, now: Any = _now) -> None:
        self.dsn, self.salon, self.now = dsn, salon, now

    # ---- reading

    def busy(
        self, stylist_id: str, start: datetime, end: datetime
    ) -> list[tuple[datetime, datetime]]:
        with connect(self.dsn) as c:
            rows = c.execute(
                "SELECT lower(period) AS s, upper(period) AS e FROM appointments"
                " WHERE stylist_id = %s AND period && tstzrange(%s, %s)"
                " AND (status = 'confirmed' OR (status = 'held' AND hold_expires > %s))",
                (stylist_id, start, end, self.now()),
            ).fetchall()
        return [(r["s"], r["e"]) for r in rows]  # type: ignore[misc]

    def find_slots(
        self,
        service_id: str,
        day_from: date,
        day_to: date,
        stylist_id: str | None = None,
        limit: int = 6,
        earliest: time | None = None,
        latest: time | None = None,
        latest_first: bool = False,
    ) -> list[Slot]:
        """Free start times where the whole service fits inside opening hours, for stylists who
        have the skill (VOICE-004). Earliest first, at most `limit`; `earliest`/`latest` (local
        start times) narrow it to the part of the day the caller asked for."""
        svc = self.salon.service(service_id)
        dur = timedelta(minutes=svc.minutes)
        stylists = [s for s in self.salon.stylists if service_id in s.skills]
        if stylist_id:
            stylists = [s for s in stylists if s.id == stylist_id]
            if not stylists:
                raise BookingError(f"{stylist_id} doesn't do {svc.name}")
        soonest = self.now() + timedelta(minutes=self.salon.min_lead_minutes)
        out: list[Slot] = []
        day = day_from
        cap = 10_000 if latest_first else limit  # latest-first: collect all, keep the last ones
        while day <= day_to and len(out) < cap:
            span = self.salon.opening(day)
            if span:
                busy = {s.id: self.busy(s.id, span[0], span[1]) for s in stylists}
                t = span[0]
                while t + dur <= span[1] and len(out) < cap:
                    local = t.astimezone(self.salon.tz).time()
                    in_window = (earliest is None or local >= earliest) and (
                        latest is None or local <= latest
                    )
                    if t >= soonest and in_window:
                        for st in stylists:
                            if all(not (t < e and s < t + dur) for s, e in busy[st.id]):
                                out.append(Slot(st.id, service_id, t, t + dur))
                                break
                    t += self.salon.step()
            day += timedelta(days=1)
        return sorted(out, key=lambda s: s.start, reverse=True)[:limit] if latest_first else out

    def get(self, appointment_id: UUID | str) -> dict[str, Any] | None:
        with connect(self.dsn) as c:
            return c.execute(
                "SELECT *, lower(period) AS start, upper(period) AS end FROM appointments"
                " WHERE id = %s",
                (str(appointment_id),),
            ).fetchone()

    def bookings_for(self, phone: str) -> list[dict[str, Any]]:
        with connect(self.dsn) as c:
            return list(
                c.execute(
                    "SELECT id, stylist_id, service_id, lower(period) AS start FROM appointments"
                    " WHERE caller_phone = %s AND status = 'confirmed' AND upper(period) > %s"
                    " ORDER BY lower(period)",
                    (phone, self.now()),
                ).fetchall()
            )

    # ---- writing

    def _validate(self, stylist_id: str, service_id: str, start: datetime) -> datetime:
        svc = self.salon.service(service_id)
        st = self.salon.stylist(stylist_id)
        if service_id not in st.skills:
            raise BookingError(f"{st.name} doesn't do {svc.name}")
        end = start + timedelta(minutes=svc.minutes)
        span = self.salon.opening(start.astimezone(self.salon.tz).date())
        if not span or start < span[0] or end > span[1]:
            raise BookingError("outside opening hours")
        if start < self.now() + timedelta(minutes=self.salon.min_lead_minutes):
            raise BookingError("too soon to book")
        return end

    def hold(
        self,
        stylist_id: str,
        service_id: str,
        start: datetime,
        call_id: str | None = None,
        replaces: UUID | str | None = None,
    ) -> UUID:
        end = self._validate(stylist_id, service_id, start)
        now = self.now()
        try:
            with connect(self.dsn) as c, c.transaction():
                # Writes for one stylist queue on this lock (hold and confirm). Without it a
                # losing hold's insert and the winner's confirm wait on each other's exclusion
                # check: a deadlock, ~1 race in 10. The constraint still decides who wins.
                c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("appt:" + stylist_id,))
                c.execute(  # expired holds stop blocking (the constraint ignores 'expired')
                    "UPDATE appointments SET status = 'expired', updated_at = now()"
                    " WHERE stylist_id = %s AND status = 'held' AND hold_expires <= %s",
                    (stylist_id, now),
                )
                row = c.execute(
                    "INSERT INTO appointments (stylist_id, service_id, period, status,"
                    " hold_expires, call_id, replaces) VALUES (%s,%s,%s,'held',%s,%s,%s)"
                    " RETURNING id",
                    (
                        stylist_id,
                        service_id,
                        Range(start, end, "[)"),
                        now + timedelta(seconds=self.salon.hold_ttl_seconds),
                        call_id,
                        str(replaces) if replaces else None,
                    ),
                ).fetchone()
                assert row is not None
                self._audit(
                    c,
                    row["id"],
                    "hold",
                    {"stylist": stylist_id, "service": service_id, "start": start.isoformat()},
                    call_id,
                )
                return UUID(str(row["id"]))
        except psycopg.errors.ExclusionViolation:
            with connect(self.dsn) as c:
                self._audit(
                    c,
                    None,
                    "hold_rejected",
                    {"stylist": stylist_id, "start": start.isoformat()},
                    call_id,
                )
            raise SlotTaken(f"{stylist_id} is not free at {start.isoformat()}") from None

    def confirm(
        self, hold_id: UUID | str, name: str, phone: str, call_id: str | None = None
    ) -> UUID:
        """Turn a live hold into a booking. If the hold replaces an earlier booking (reschedule),
        that booking is cancelled in the same transaction."""
        with connect(self.dsn) as c, c.transaction():
            sty = c.execute(
                "SELECT stylist_id FROM appointments WHERE id = %s", (str(hold_id),)
            ).fetchone()
            if sty:  # same lock as hold(): the update re-checks the exclusion constraint, which
                # would otherwise wait on a racing hold's insert while it waits on us (deadlock)
                c.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    ("appt:" + str(sty["stylist_id"]),),
                )
            row = c.execute(
                "SELECT status, hold_expires, replaces FROM appointments WHERE id = %s FOR UPDATE",
                (str(hold_id),),
            ).fetchone()
            if row is None or row["status"] != "held" or row["hold_expires"] <= self.now():
                raise BookingError("hold expired or unknown: find the slot again")
            c.execute(
                "UPDATE appointments SET status = 'confirmed', hold_expires = NULL,"
                " caller_name = %s, caller_phone = %s, updated_at = now() WHERE id = %s",
                (name, phone, str(hold_id)),
            )
            if row["replaces"]:
                c.execute(
                    "UPDATE appointments SET status = 'cancelled', updated_at = now()"
                    " WHERE id = %s AND status = 'confirmed'",
                    (row["replaces"],),
                )
                self._audit(c, row["replaces"], "cancel", {"reason": "rescheduled"}, call_id)
            self._audit(c, hold_id, "reschedule" if row["replaces"] else "confirm", {}, call_id)
        return UUID(str(hold_id))

    def cancel(self, booking_id: UUID | str, call_id: str | None = None) -> None:
        with connect(self.dsn) as c, c.transaction():
            row = c.execute(
                "UPDATE appointments SET status = 'cancelled', updated_at = now()"
                " WHERE id = %s AND status = 'confirmed' RETURNING id",
                (str(booking_id),),
            ).fetchone()
            if row is None:
                raise BookingError("no confirmed booking with that id")
            self._audit(c, booking_id, "cancel", {}, call_id)

    def audit(self, appointment_id: UUID | str) -> list[dict[str, Any]]:
        with connect(self.dsn) as c:
            return list(
                c.execute(
                    "SELECT action, data, call_id, at FROM booking_audit WHERE appointment_id = %s"
                    " ORDER BY id",
                    (str(appointment_id),),
                ).fetchall()
            )

    @staticmethod
    def _audit(c: Any, appt: Any, action: str, data: dict[str, Any], call_id: str | None) -> None:
        c.execute(
            "INSERT INTO booking_audit (appointment_id, action, data, call_id)"
            " VALUES (%s, %s, %s, %s)",
            (str(appt) if appt else None, action, json.dumps(data), call_id),
        )

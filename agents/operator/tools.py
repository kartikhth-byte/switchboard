"""Operator tools over the calendar of record (VOICE-001, 003, 004). Code enforces the rules:
skills, durations, hours and overlaps are checked by the calendar, not the model."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agents.operator.calendar import BookingError, Calendar, SlotTaken
from agents.operator.notify import Notifier
from agents.operator.when import NotUnderstood, Window, resolve_day, resolve_time, understood
from core.tools import ToolContext, ToolRegistry, ToolResult, ToolSpec

AGENT = "operator"


@dataclass
class CallState:
    call_id: str
    caller_phone: str | None = None
    holds: dict[str, str] = field(default_factory=dict)  # hold_id -> replaces booking id or ""
    booked: list[str] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    transferred: str | None = None  # reason


class NoInput(BaseModel):
    pass


class FindSlotsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")  # a stale parameter is an error, not ignored

    service_id: str
    day: str | None = Field(
        default=None,
        description="the caller's own words for the day, unchanged: 'this Sunday', 'tomorrow', "
        "'Friday the 23rd'. Code resolves it; don't convert it to a date yourself.",
    )
    time: str | None = Field(
        default=None,
        description="the caller's own words for the time, unchanged: '8 in the morning', "
        "'around 4', 'after work', 'as late as possible'. Omit if they gave none.",
    )
    stylist_id: str | None = None
    date_from: date | None = Field(default=None, description="only for a range of days")
    date_to: date | None = None


class HoldIn(BaseModel):
    service_id: str
    stylist_id: str
    start: datetime = Field(description="ISO start time from find_slots")
    replaces_booking_id: str | None = Field(default=None, description="when rescheduling")


class ConfirmIn(BaseModel):
    hold_id: str
    name: str = Field(min_length=1, max_length=80)
    phone: str | None = Field(default=None, description="omit to use the caller-ID number")


class PhoneIn(BaseModel):
    phone: str | None = Field(default=None, description="omit to use the caller-ID number")


class BookingIdIn(BaseModel):
    booking_id: str


class TransferIn(BaseModel):
    reason: str = Field(max_length=200)


def _is_bare_weekday(phrase: str) -> bool:
    p = phrase.lower()
    return not any(w in p for w in ("today", "this", "tonight"))


def build_registry(cal: Calendar, st: CallState, notifier: Notifier | None = None) -> ToolRegistry:
    salon = cal.salon
    reg = ToolRegistry()

    def get_services(_: NoInput, ctx: ToolContext) -> dict[str, Any]:
        return {
            "services": [s.model_dump() for s in salon.services],
            "stylists": [{"id": s.id, "name": s.name, "does": s.skills} for s in salon.stylists],
        }

    def find_slots(inp: FindSlotsIn, ctx: ToolContext) -> dict[str, Any]:
        today = cal.now().astimezone(salon.tz).date()
        try:  # the caller's words become dates and times here, in code (not in the model)
            day_from = resolve_day(inp.day, today) if inp.day else inp.date_from
            if inp.day and day_from == today and _is_bare_weekday(inp.day):
                # "Sunday", said on a Sunday after the last bookable time, means next Sunday;
                # earlier in the day it still means today. ("this Sunday"/"today" stay today.)
                span_today = salon.opening(today)
                last_start = (
                    span_today[1] - timedelta(minutes=salon.min_lead_minutes)
                    if (span_today)
                    else None
                )
                if last_start is None or cal.now() >= last_start:
                    day_from = today + timedelta(days=7)
            if day_from is None:
                return {"error": "say which day: pass the caller's words in `day`"}
            day_to = day_from if inp.day else (inp.date_to or day_from)
            span = salon.opening(day_from)
            win = (
                resolve_time(
                    inp.time, span[0].time() if span else None, span[1].time() if span else None
                )
                if inp.time
                else Window(None, None)
            )
        except NotUnderstood as exc:
            return {
                "error": f"didn't understand {str(exc)!r}: ask the caller to say it another way"
            }
        try:
            slots = cal.find_slots(
                inp.service_id,
                day_from,
                day_to,
                inp.stylist_id,
                limit=4,
                earliest=win.earliest,
                latest=win.latest,
                latest_first=win.latest_first,
            )
        except (BookingError, KeyError) as exc:
            return {"error": str(exc).strip("'")}
        hours = {}
        d = day_from
        while d <= day_to:
            sp = salon.opening(d)
            hours[d.isoformat()] = f"{sp[0]:%H:%M}-{sp[1]:%H:%M}" if sp else "closed"
            d += timedelta(days=1)
        out: dict[str, Any] = {
            "understood": understood(salon, day_from, win),
            "opening_hours": hours,
        }
        if slots:
            out["slots"] = [x.as_dict(salon) for x in slots]
            return out
        out["slots"] = []
        if win.earliest or win.latest:
            # Nothing in the window they asked for: hand back the nearest real alternatives, so
            # the reply offers times that exist instead of guessing one (live call 2026-10-21).
            nearest = cal.find_slots(inp.service_id, day_from, day_to, inp.stylist_id, limit=3)
            out["nearest_free"] = [x.as_dict(salon) for x in nearest]
        out["note"] = "nothing free in that window; offer only times listed here"
        return out

    def mine() -> set[str]:
        phone = st.caller_phone
        return {str(r["id"]) for r in cal.bookings_for(phone)} | set(st.booked) if phone else set()

    def hold_slot(inp: HoldIn, ctx: ToolContext) -> ToolResult:
        start = inp.start if inp.start.tzinfo else inp.start.replace(tzinfo=salon.tz)
        if inp.replaces_booking_id and inp.replaces_booking_id not in mine():
            return ToolResult(ok=False, error="not one of this caller's bookings")
        try:
            hid = cal.hold(
                inp.stylist_id, inp.service_id, start, st.call_id, replaces=inp.replaces_booking_id
            )
        except SlotTaken:
            return ToolResult(ok=False, error="slot_taken: someone just took that time")
        except (BookingError, KeyError) as exc:
            return ToolResult(ok=False, error=str(exc).strip("'"))
        st.holds[str(hid)] = inp.replaces_booking_id or ""
        return ToolResult(
            ok=True, payload={"hold_id": str(hid), "held_for_seconds": salon.hold_ttl_seconds}
        )

    def confirm_booking(inp: ConfirmIn, ctx: ToolContext) -> ToolResult:
        phone = inp.phone or st.caller_phone
        if not phone:
            return ToolResult(ok=False, error="need a phone number")
        if inp.hold_id not in st.holds:
            return ToolResult(ok=False, error="unknown hold_id: use the id hold_slot returned")
        try:
            bid = cal.confirm(inp.hold_id, inp.name, phone, st.call_id)
        except BookingError as exc:
            return ToolResult(ok=False, error=str(exc))
        st.booked.append(str(bid))
        if st.holds[inp.hold_id]:
            st.cancelled.append(st.holds[inp.hold_id])
        if notifier is not None:  # VOICE-005: confirmation now, reminder at T-24h
            notifier.booked(bid, replaced=st.holds[inp.hold_id] or None)
        b = cal.get(bid) or {}
        return ToolResult(
            ok=True,
            payload={
                "booking_id": str(bid),
                "status": "confirmed",
                "start": b["start"].astimezone(salon.tz).isoformat(),
            },
        )

    def find_my_bookings(inp: PhoneIn, ctx: ToolContext) -> dict[str, Any]:
        phone = inp.phone or st.caller_phone
        if not phone:
            return {"error": "need a phone number"}
        rows = cal.bookings_for(phone)
        return {
            "bookings": [
                {
                    "booking_id": str(r["id"]),
                    "service_id": r["service_id"],
                    "stylist_id": r["stylist_id"],
                    "stylist": salon.stylist(r["stylist_id"]).name,
                    "start": r["start"].astimezone(salon.tz).isoformat(),
                }
                for r in rows
            ]
        }

    def cancel_booking(inp: BookingIdIn, ctx: ToolContext) -> ToolResult:
        if inp.booking_id not in mine():
            return ToolResult(ok=False, error="not one of this caller's bookings")
        try:
            cal.cancel(inp.booking_id, st.call_id)
        except BookingError as exc:
            return ToolResult(ok=False, error=str(exc))
        st.cancelled.append(inp.booking_id)
        if notifier is not None:
            notifier.cancelled(inp.booking_id)
        return ToolResult(ok=True, payload={"cancelled": inp.booking_id})

    def transfer_to_staff(inp: TransferIn, ctx: ToolContext) -> ToolResult:
        st.transferred = inp.reason
        return ToolResult(ok=True, payload={"transferring_to": "salon staff"})

    specs: list[tuple[str, str, type[BaseModel], str, Any]] = [
        (
            "get_services",
            "Services with durations and prices; stylists and what they do.",
            NoInput,
            "read",
            get_services,
        ),
        (
            "find_slots",
            "Free start times for a service, optionally one stylist, over a date range.",
            FindSlotsIn,
            "read",
            find_slots,
        ),
        (
            "hold_slot",
            "Hold a slot from find_slots for 2 minutes while the caller confirms. Pass "
            "replaces_booking_id to reschedule.",
            HoldIn,
            "write",
            hold_slot,
        ),
        (
            "confirm_booking",
            "Confirm a held slot with the caller's name.",
            ConfirmIn,
            "write",
            confirm_booking,
        ),
        ("find_my_bookings", "The caller's upcoming bookings.", PhoneIn, "read", find_my_bookings),
        (
            "cancel_booking",
            "Cancel one of the caller's bookings.",
            BookingIdIn,
            "write",
            cancel_booking,
        ),
        ("transfer_to_staff", "Hand the call to a person.", TransferIn, "write", transfer_to_staff),
    ]
    for name, desc, model, perm, fn in specs:
        reg.register(
            ToolSpec(
                name=name,
                description=desc,
                input_model=model,
                permission=perm,  # type: ignore[arg-type]
                side_effect=perm == "write",
            ),
            fn,
            agents=[AGENT],
        )
    return reg

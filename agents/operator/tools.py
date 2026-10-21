"""Operator tools over the calendar of record (VOICE-001, 003, 004). Code enforces the rules:
skills, durations, hours and overlaps are checked by the calendar, not the model."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from pydantic import BaseModel, Field

from agents.operator.calendar import BookingError, Calendar, SlotTaken
from agents.operator.notify import Notifier
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
    service_id: str
    date_from: date
    date_to: date | None = None
    stylist_id: str | None = None
    earliest_time: time | None = Field(
        default=None, description="HH:MM local; set when the caller names a time or 'afternoon'"
    )
    latest_time: time | None = Field(default=None, description="HH:MM local, latest start")
    latest_first: bool = Field(
        default=False, description="true when they want the latest time ('as late as possible')"
    )


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


def build_registry(cal: Calendar, st: CallState, notifier: Notifier | None = None) -> ToolRegistry:
    salon = cal.salon
    reg = ToolRegistry()

    def get_services(_: NoInput, ctx: ToolContext) -> dict[str, Any]:
        return {
            "services": [s.model_dump() for s in salon.services],
            "stylists": [{"id": s.id, "name": s.name, "does": s.skills} for s in salon.stylists],
        }

    def find_slots(inp: FindSlotsIn, ctx: ToolContext) -> dict[str, Any]:
        try:
            slots = cal.find_slots(
                inp.service_id,
                inp.date_from,
                inp.date_to or inp.date_from,
                inp.stylist_id,
                limit=4,
                earliest=inp.earliest_time,
                latest=inp.latest_time,
                latest_first=inp.latest_first,
            )
        except (BookingError, KeyError) as exc:
            return {"error": str(exc).strip("'")}
        day_to = inp.date_to or inp.date_from
        hours = {}
        d = inp.date_from
        while d <= day_to:  # the model must not work out opening times itself (live call 10-21)
            span = salon.opening(d)
            hours[d.isoformat()] = f"{span[0]:%H:%M}-{span[1]:%H:%M}" if span else "closed"
            d += timedelta(days=1)
        out: dict[str, Any] = {"opening_hours": hours}
        if slots:
            out["slots"] = [s.as_dict(salon) for s in slots]
            return out
        out["slots"] = []
        if inp.earliest_time or inp.latest_time:
            # Nothing in the window they asked for: hand back the nearest real alternatives, so
            # the reply offers times that exist instead of guessing one.
            nearest = cal.find_slots(inp.service_id, inp.date_from, day_to, inp.stylist_id, limit=3)
            out["nearest_free"] = [s.as_dict(salon) for s in nearest]
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

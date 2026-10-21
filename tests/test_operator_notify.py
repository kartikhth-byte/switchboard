"""VOICE-005: confirmation queued at booking, reminder at T-24h, both cancelled on cancel or
reschedule; sends follow the outbound rules and are idempotent."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agents.operator.calendar import Calendar
from agents.operator.notify import Notifier, Sender
from agents.operator.salon import load_salon
from core.config.settings import Settings, get_settings
from core.db import connect, migrate
from core.jobs import JobQueue
from integrations.messaging import MockEmail, MockWhatsApp

pytestmark = pytest.mark.integration
SALON = load_salon()
TZ = SALON.tz
NOW = datetime(2027, 1, 5, 11, 0, tzinfo=TZ)
DSN = get_settings().database_url


@pytest.fixture
def cal() -> Calendar:
    migrate(DSN)
    with connect(DSN) as c:
        c.execute("TRUNCATE booking_audit, appointments")
        c.execute("DELETE FROM jobs WHERE key LIKE 'appt:%'")
    return Calendar(DSN, SALON, now=lambda: NOW)


def book(cal: Calendar, day: int, hour: int, phone: str = "+919800000042") -> str:
    start = datetime(2027, 1, day, hour, 0, tzinfo=TZ)
    return str(cal.confirm(cal.hold("farhan", "haircut_men", start), "Ravi Kumar", phone))


def jobs(prefix: str) -> dict[str, dict]:  # type: ignore[type-arg]
    with connect(DSN) as c:
        rows = c.execute(
            "SELECT key, status, run_at FROM jobs WHERE key LIKE %s", (prefix + "%",)
        ).fetchall()
    return {r["key"].split(":")[-1]: r for r in rows}


def test_booking_queues_confirmation_now_and_reminder_24h_before(cal: Calendar) -> None:
    b = book(cal, 8, 15)  # Friday 15:00
    Notifier(cal, JobQueue(DSN)).booked(b)
    j = jobs(f"appt:{b}:")
    assert j["confirm"]["status"] == "pending"
    assert j["confirm"]["run_at"] <= datetime.now(UTC) + timedelta(minutes=1)  # within 1 min
    assert j["reminder"]["run_at"] == datetime(2027, 1, 7, 15, 0, tzinfo=TZ)


def test_cancel_and_reschedule_cancel_the_old_jobs(cal: Calendar) -> None:
    n = Notifier(cal, JobQueue(DSN))
    a = book(cal, 8, 15)
    n.booked(a)
    assert n.cancelled(a) == 2
    assert {v["status"] for v in jobs(f"appt:{a}:").values()} == {"cancelled"}
    old = book(cal, 9, 10)
    n.booked(old)
    new = str(
        cal.confirm(
            cal.hold("farhan", "haircut_men", datetime(2027, 1, 9, 12, tzinfo=TZ), replaces=old),
            "Ravi",
            "+919800000042",
        )
    )
    n.booked(new, replaced=old)
    assert {v["status"] for v in jobs(f"appt:{old}:").values()} == {"cancelled"}
    assert jobs(f"appt:{new}:")["reminder"]["status"] == "pending"


def test_no_reminder_when_the_booking_is_under_24h_away(cal: Calendar) -> None:
    b = book(cal, 6, 10)  # tomorrow 10:00, 23h away
    Notifier(cal, JobQueue(DSN)).booked(b)
    assert set(jobs(f"appt:{b}:")) == {"confirm"}


def sender(cal: Calendar, mode: str, allow: str) -> tuple[Sender, MockWhatsApp, MockEmail]:
    s = Settings(_env_file=None, database_url=DSN, outbound_mode=mode, outbound_allowlist=allow)  # type: ignore[call-arg]
    wa, em = MockWhatsApp(), MockEmail()
    return Sender(cal, s, wa, em), wa, em


def test_sender_follows_outbound_rules_and_is_idempotent(cal: Calendar) -> None:
    b = book(cal, 8, 15)
    off, wa0, em0 = sender(cal, "off", "")
    assert off.send(b, "confirmation") == "blocked" and not wa0.sent and not em0.sent
    s, wa, em = sender(cal, "allowlist", "+9198000*,@studionine.test")
    assert s.send(b, "confirmation") == "sent"
    assert wa.sent[0]["template"] == SALON.notifications.whatsapp_template
    assert wa.sent[0]["params"][0] == "Ravi" and "Friday 8 January" in wa.sent[0]["params"][2]
    assert "Men's haircut" in em.sent[0]["subject"]
    assert s.send(b, "confirmation") == "already sent" and len(wa.sent) == 1  # job retry
    cal.cancel(b)
    assert s.send(b, "reminder") == "skipped"


def test_recipients_outside_the_allowlist_get_nothing(cal: Calendar) -> None:
    b = book(cal, 8, 15, phone="+14155550100")
    s, wa, em = sender(cal, "allowlist", "+9198000*")  # front desk email not allowed either
    assert s.send(b, "confirmation") == "blocked" and not wa.sent and not em.sent


def test_tools_queue_and_cancel_notifications(cal: Calendar) -> None:
    from uuid import uuid4

    from agents.operator.tools import CallState, build_registry
    from core.tools import ToolContext

    st = CallState("t-call", "+919800000042")
    reg = build_registry(cal, st, Notifier(cal, JobQueue(DSN)))
    ctx = ToolContext(run_id=uuid4(), agent="operator")
    hold = reg.execute(
        "hold_slot",
        {"service_id": "haircut_men", "stylist_id": "farhan", "start": "2027-01-08T15:00:00+05:30"},
        ctx,
    )
    done = reg.execute("confirm_booking", {"hold_id": hold.payload["hold_id"], "name": "Ravi"}, ctx)
    bid = done.payload["booking_id"]
    assert set(jobs(f"appt:{bid}:")) == {"confirm", "reminder"}
    reg.execute("cancel_booking", {"booking_id": bid}, ctx)
    assert {v["status"] for v in jobs(f"appt:{bid}:").values()} == {"cancelled"}

"""Runs one Operator eval case: a simulated caller (OpenAI) phones the real CallSession (Claude)
with the real calendar, frozen at the case's own week. Pre-existing bookings are seeded first;
the case's week is cleared before each run so re-runs start from the same state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from uuid import uuid4

from agents.operator.calendar import Calendar
from agents.operator.salon import Salon
from agents.operator.session import CallSession
from core.config.agent import AgentConfig
from core.db import connect
from core.evals import AgentOutput, EvalCase, SimulatedUser, run_conversation
from core.evals.models import Persona
from core.memory import ConversationStore
from core.models import ModelClient
from core.runs import RunStore


@dataclass
class EvalEnv:
    cfg: AgentConfig
    salon: Salon
    dsn: str
    client: ModelClient
    sim_client: ModelClient
    runs: RunStore
    langfuse: Any = None


def _clock(case: EvalCase) -> datetime:
    week = date.fromisoformat(case.expected.get("week") or (case.input or {})["week"])
    if case.expected.get("after_hours"):
        return datetime.combine(week - timedelta(days=1), time(21, 0))  # Monday 9pm
    return datetime.combine(week, time(11, 0))  # Tuesday 11am


def _reset_week(env: EvalEnv, week: date) -> None:
    lo = datetime.combine(week - timedelta(days=2), time(0), env.salon.tz)
    hi = datetime.combine(week + timedelta(days=9), time(0), env.salon.tz)
    with connect(env.dsn) as c:
        c.execute(
            "UPDATE appointments SET status = 'cancelled', updated_at = now()"
            " WHERE status IN ('held','confirmed') AND period && tstzrange(%s, %s)",
            (lo, hi),
        )


@dataclass
class Prepared:
    call: CallSession
    store: ConversationStore
    phone: str
    seeds: list[str]
    truth: dict[str, Any] | None = None  # the calendar before the call (claims.py)


def prepare(env: EvalEnv, case: EvalCase, kind: str = "eval") -> Prepared:
    """Freeze the clock at the case's week, clear the week, seed bookings, open a CallSession.
    Shared by the text suite and the audio harness (agents/operator/evals/audio.py)."""
    now = _clock(case).replace(tzinfo=env.salon.tz)
    cal = Calendar(env.dsn, env.salon, now=lambda: now)
    week = date.fromisoformat(case.expected.get("week") or (case.input or {})["week"])
    _reset_week(env, week)
    seeds = []
    for sd in case.expected.get("seed", []):
        start = datetime.combine(
            date.fromisoformat(sd["date"]), time.fromisoformat(sd["time"]), env.salon.tz
        )
        hid = cal.hold(sd["stylist_id"], sd["service_id"], start, call_id="eval-seed")
        seeds.append(str(cal.confirm(hid, sd["name"], sd["phone"], call_id="eval-seed")))
    phone = case.expected.get("phone") or (case.input or {})["phone"]
    store = ConversationStore(env.dsn, "operator")
    call = CallSession(
        env.cfg,
        cal,
        env.client,
        env.runs,
        call_id=f"{kind}-{case.id}-{uuid4().hex[:6]}",
        caller_phone=phone,
        transcripts=store,
        langfuse=env.langfuse,
        run_kind="eval",
    )
    return Prepared(call, store, phone, seeds, _truth(env, cal, case, week, now))


def _truth(
    env: EvalEnv, cal: Calendar, case: EvalCase, week: date, now: datetime
) -> dict[str, Any] | None:
    """Snapshot what was really free before the caller spoke, for the case's service: the
    ground truth `times_grounded` checks every time the agent says against."""
    svc = case.expected.get("service_id")
    if not svc:
        return None
    lo, hi = week - timedelta(days=2), week + timedelta(days=9)

    def by_day(slots: list[Any]) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for x in sorted(slots, key=lambda x: x.start):
            local = x.start.astimezone(env.salon.tz)
            out.setdefault(str(local.date()), []).append(f"{local:%H:%M}")
        return out

    hours: dict[str, list[str]] = {}
    d = lo
    while d <= hi:
        sp = env.salon.opening(d)
        if sp:
            hours[str(d)] = [f"{sp[0]:%H:%M}", f"{sp[1]:%H:%M}"]
        d += timedelta(days=1)
    skilled = [st for st in env.salon.stylists if svc in st.skills]
    return {
        "today": str(now.date()),
        "case_day": case.expected.get("date"),
        "free": by_day(cal.find_slots(svc, lo, hi, None, limit=10_000)),
        "by_stylist": {
            st.id: by_day(cal.find_slots(svc, lo, hi, st.id, limit=10_000)) for st in skilled
        },
        "hours": hours,
        "stylists": {st.name.lower(): st.id for st in env.salon.stylists},
    }


def collect(
    env: EvalEnv,
    p: Prepared,
    greeting: str,
    turns: list[Any],
    transcript: list[dict[str, str]],
    outcome: str,
    extra_cost: float = 0.0,
    extra: dict[str, Any] | None = None,
) -> AgentOutput:
    """Read the calendar back and package everything the assertions need."""
    with connect(env.dsn) as c:
        rows = c.execute(
            "SELECT id::text, stylist_id, service_id, status, lower(period) AS start"
            " FROM appointments WHERE (caller_phone = %s OR id::text = ANY(%s) OR call_id = %s)"
            " AND status IN ('confirmed','cancelled') ORDER BY created_at",
            # call_id: a booking made in this call under another number the caller gave
            # (the prompt allows it) still counts; looking up by caller ID alone missed it.
            (p.phone, p.seeds, p.call.call_id),
        ).fetchall()
    bookings = [{**r, "start": r["start"].astimezone(env.salon.tz).isoformat()} for r in rows]
    truth = None
    if p.truth is not None:  # every booking this call touched is a time the agent may name
        truth = {**p.truth, "bookings": sorted({b["start"][11:16] for b in bookings})}
    return AgentOutput(
        answer=turns[-1].text if turns else "",
        transcript=transcript,
        cost_usd=sum(t.cost_usd for t in turns) + extra_cost,
        data={
            "greeting": greeting,
            "agent_turns": [t.text for t in turns],
            "tools": [x for t in turns for x in t.tools],
            "tool_log": [x for t in turns for x in t.tool_log],
            "transferred": p.call.state.transferred,
            "seeds": p.seeds,
            "bookings": bookings,
            "stored_transcript": p.store.load(p.call.call_id),
            "outcome": outcome,
            "truth": truth,
            **(extra or {}),
        },
    )


def run_target(env: EvalEnv, case: EvalCase) -> AgentOutput:
    p = prepare(env, case)
    call = p.call
    greeting = call.greet()
    turns: list[Any] = []
    sim_cost = 0.0

    def agent_turn(msg: str) -> str:
        t = call.respond(msg)
        turns.append(t)
        return t.text

    if case.persona is not None:
        user = SimulatedUser(Persona.model_validate(case.persona), env.sim_client)
        transcript, outcome = run_conversation(agent_turn, user)
        sim_cost = user.cost_usd
    else:
        assert case.input is not None
        agent_turn(case.input["message"])
        transcript, outcome = [], "scripted"
    return collect(env, p, greeting, turns, transcript, outcome, sim_cost)

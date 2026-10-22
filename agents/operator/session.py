"""One phone call (text in, text out). The same `CallSession` serves the text-mode eval suite
and the Pipecat voice pipeline, which only adds speech-to-text in front and text-to-speech
behind (spec §7.1), so the evals test exactly what callers talk to.

Decided in code, not by the model:
- the opening line: AI + recording disclosure, and the after-hours line by config hours
  (VOICE-017, VOICE-002);
- transfer to staff when the caller asks for a person or complains, or after N consecutive
  turns where a booking step failed (VOICE-002);
- the stored transcript is redacted (VOICE-017); booking actions are in `booking_audit`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from agents.operator.calendar import Calendar
from agents.operator.notify import Notifier
from agents.operator.tools import AGENT, CallState, build_registry
from core.config.agent import AgentConfig
from core.memory import ConversationStore
from core.models import Message, ModelClient
from core.redaction import redact
from core.runs import RunStore, start_run
from core.runs.models import RunKind

BOOKING_TOOLS = {"find_slots", "hold_slot", "confirm_booking", "cancel_booking"}
FILLER = "One moment while I check that."


@dataclass
class Turn:
    text: str
    tools: list[str] = field(default_factory=list)
    tool_log: list[dict[str, Any]] = field(default_factory=list)  # args + result (evals, debug)
    transferred: bool = False
    used_filler: bool = False
    cost_usd: float = 0.0
    aborted: bool = False  # the caller resumed speaking; nothing here should be said


@dataclass
class CallSession:
    cfg: AgentConfig
    cal: Calendar
    client: ModelClient
    runs: RunStore
    call_id: str
    caller_phone: str | None = None
    transcripts: ConversationStore | None = None
    langfuse: Any = None
    notifier: Notifier | None = None  # confirmations + reminders (VOICE-005)
    run_kind: RunKind = "live"
    messages: list[Message] = field(default_factory=list)
    failed_streak: int = 0
    _pending: str = ""
    last_input: str = ""  # an utterance cut short by early turn detection, joined to the next
    turns: list[dict[str, str]] = field(default_factory=list)
    on_tools: Any = None  # voice: called once when a turn starts using tools (say the filler)

    def __post_init__(self) -> None:
        self.state = CallState(self.call_id, self.caller_phone)
        self.registry = build_registry(self.cal, self.state, self.notifier)

    # ---- opening line (code)

    def greet(self) -> str:
        r = self.cfg.rules
        line = str(r["disclosure"])
        if not self.cal.salon.is_open(self.cal.now()):
            line += " " + str(r["after_hours"])
        line += " How can I help?"
        self._record("agent", line)
        return line

    # ---- a caller turn

    def _wants_person(self, text: str) -> bool:
        t = text.lower()
        return any(re.search(rf"\b{re.escape(p)}\b", t) for p in self.cfg.rules["transfer_phrases"])

    def _system(self) -> str:
        now = self.cal.now().astimezone(self.cal.salon.tz)
        days = []
        for i in range(14):  # date maths is where small models slip: give them the calendar
            d = (now + timedelta(days=i)).date()
            label = "today · " if i == 0 else "tomorrow · " if i == 1 else ""
            span = self.cal.salon.opening(d)
            hours = f"open {span[0]:%H:%M}-{span[1]:%H:%M}" if span else "CLOSED"
            days.append(f"{d:%a %d %b} = {d.isoformat()} · {label}{hours}")
        return self.cfg.prompt(
            "system",
            days="\n".join(days),
            now=now.strftime("%A %d %B %Y, %I:%M %p"),
            open_now="open" if self.cal.salon.is_open(now) else "closed",
            caller_phone="known" if self.caller_phone else "not available",
        )

    def respond(self, text: str, should_stop: Callable[[], bool] | None = None) -> Turn:
        """One caller turn. `should_stop` (voice) says the caller has started speaking again:
        the turn detector ended their turn too early. Like LiveKit's preemptive generation and
        Smart Turn's guidance, we then stop at the next step instead of finishing a reply nobody
        will hear. If no tool ran yet, the words are kept and the next turn restarts from the
        whole utterance; if one did, its results stay and the next words follow them."""
        self._record("caller", text)
        if self._pending:
            text, self._pending = f"{self._pending} {text}", ""
        self.last_input = text  # what this turn actually answers (harness reporting)
        if self.state.transferred:
            return self._say(Turn("I'm connecting you to the salon team now.", transferred=True))
        if self._wants_person(text):
            self.state.transferred = "caller asked for a person / complaint"
            return self._say(
                Turn(
                    "Of course, I'll put you through to someone at the salon now.", transferred=True
                )
            )
        if not self.messages:
            self.messages = [{"role": "system", "content": self._system()}]
        self.messages.append({"role": "user", "content": text})
        mark = len(self.messages) - 1
        turn = Turn("")
        failed = False

        def stop() -> bool:
            if should_stop is None or not should_stop():
                return False
            turn.aborted = True
            if not turn.tools:  # nothing done yet: restart from the whole utterance next time
                del self.messages[mark:]
                self._pending = text
            return True

        with start_run(
            AGENT,
            self.runs,
            self.langfuse,
            run_input={"caller": redact(text)},
            session_id=self.call_id,
            budget=self.cfg.budget(None),
            kind=self.run_kind,
        ) as run:
            schemas = self.registry.schemas_for(AGENT)
            for step in range(self.cfg.limits.max_steps):
                if stop():
                    break
                resp = run.model(self.client, self.messages, schemas, name=f"step-{step}")
                if stop():  # the model call can't be cancelled, but its result can be dropped
                    break
                if not resp.tool_calls:
                    turn.text = (resp.content or "").strip()
                    self.messages.append({"role": "assistant", "content": turn.text})
                    break
                if not turn.used_filler and self.on_tools is not None:
                    self.on_tools()
                turn.used_filler = True  # the voice pipeline says FILLER while tools run
                self.messages.append(
                    {
                        "role": "assistant",
                        "content": resp.content or "",
                        "tool_calls": [
                            {
                                "id": c.id,
                                "type": "function",
                                "function": {"name": c.name, "arguments": c.raw_arguments or "{}"},
                            }
                            for c in resp.tool_calls
                        ],
                    }
                )
                for call in resp.tool_calls:
                    turn.tools.append(call.name)
                    res = run.tool(self.registry, call.name, call.arguments)
                    turn.tool_log.append(
                        {
                            "tool": call.name,
                            "args": call.arguments,
                            "ok": res.ok,
                            "error": res.error,
                            "result": str(res.payload)[:400],
                        }
                    )
                    if call.name in BOOKING_TOOLS and not res.ok:
                        failed = True
                    body = res.model_dump_json(include={"ok", "payload", "error"})
                    self.messages.append({"role": "tool", "tool_call_id": call.id, "content": body})
            turn.cost_usd = run.cost_usd
        if turn.aborted:
            return turn
        if run.status == "budget_exceeded":
            self.state.transferred = "call token budget exceeded"
        if not turn.text:
            turn.text = "Sorry, I didn't catch that. Could you say it again?"
            failed = True
        self.failed_streak = self.failed_streak + 1 if failed else 0
        if self.failed_streak >= int(self.cfg.rules.get("max_failed_intents", 2)):
            self.state.transferred = "repeated failed booking steps"
        if self.state.transferred:
            turn.transferred = True
            if "connect" not in turn.text.lower() and "put you through" not in turn.text.lower():
                turn.text += " Let me put you through to someone at the salon."
        return self._say(turn)

    def unsay(self) -> None:
        """The voice layer dropped the last reply because the caller kept talking (they paused
        mid-thought and the turn detector ended the turn early). Forget the unheard text so the
        next turn reads as the caller's continuation. Tool calls and their results stay: what
        was done (a hold, say) is real and the model must still see it."""
        m = self.messages[-1] if self.messages else None
        if m and m["role"] == "assistant" and not m.get("tool_calls"):
            self.messages.pop()
            if self.turns and self.turns[-1]["role"] == "agent":
                self.turns[-1]["role"] = "agent_unheard"

    # ---- transcript (redacted before it's stored)

    def _say(self, turn: Turn) -> Turn:
        self._record("agent", turn.text)
        return turn

    def _record(self, who: str, text: str) -> None:
        self.turns.append({"role": who, "text": redact(text), "at": datetime.now().isoformat()})
        if self.transcripts is not None:
            self.transcripts.save(
                self.call_id,
                {
                    "turns": self.turns,
                    "transferred": self.state.transferred,
                    "booked": self.state.booked,
                    "cancelled": self.state.cancelled,
                },
            )

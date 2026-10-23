"""Operator voice pipeline (Pipecat 1.13): browser WebRTC for development, a Twilio phone number
for real calls (`make phone`), through the same bot via Pipecat's runner.

    transport.in → Deepgram STT → user turn aggregator (Silero VAD, barge-in)
      → OperatorBrain (our CallSession: the same code the text evals test)
      → Cartesia TTS → transport.out

The brain stands where an LLM service normally sits: it receives the aggregated user turn
(LLMContextFrame) and answers with LLMText frames. It ignores speculative contexts (Pipecat may
run inference before the turn is final), says a short filler when the turn needs tools, and
drops a reply if the caller barged in while it was being prepared.

    make voice                    # browser: http://localhost:7860/client
    make phone HOST=<tunnel host> # Twilio: point the number's voice webhook at https://HOST/
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from loguru import logger
from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSSpeakFrame,
    TTSTextFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams
from pipecat.turns.user_stop.turn_analyzer_user_turn_stop_strategy import (
    TurnAnalyzerUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from agents.operator.calendar import Calendar
from agents.operator.notify import Notifier
from agents.operator.salon import load_salon
from agents.operator.session import FILLER, CallSession
from core.config.agent import load_agent_config
from core.config.settings import get_settings
from core.db import migrate
from core.jobs import JobQueue
from core.memory import ConversationStore
from core.models import ModelClient
from core.runs import PgRunStore
from core.tracing import make_langfuse
from integrations.telephony import TwilioCalls


def _text(m: Any) -> str:
    c = m.get("content")
    if isinstance(c, str):
        return c.strip()
    if isinstance(c, list):  # content parts
        return " ".join(p.get("text", "") for p in c if isinstance(p, dict)).strip()
    return ""


def new_user_text(context: LLMContext, seen: dict[int, Any]) -> str:
    """Everything the caller said that the brain hasn't handed to the session yet. One turn can
    hold several user messages (a transcript finalised before the turn started, say); reading
    only the last one dropped the start of what they said. Messages are tracked by identity,
    not position, so a trimmed or replaced context can't skip or replay a turn. `seen` maps
    id -> the message itself: holding the reference keeps the id from being reused after the
    context lets the message go (review 2: a bare set of ids missed a real turn)."""
    new = [m for m in context.get_messages() if isinstance(m, dict) and id(m) not in seen]
    seen.update((id(m), m) for m in new)
    said = [_text(m) for m in new if m.get("role") == "user"]
    return " ".join(t for t in said if t)


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


class SpokenWatcher(FrameProcessor):
    """Sits after the output transport. Pipecat releases each TTS word (TTSTextFrame) there at
    the moment it is spoken, so this is the one place that knows what the caller has actually
    heard; it tells the brain."""

    def __init__(self, brain: OperatorBrain) -> None:
        super().__init__()
        self.brain = brain

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, TTSTextFrame) and direction == FrameDirection.DOWNSTREAM:
            self.brain.heard(frame.text)
        await self.push_frame(frame, direction)


class OperatorBrain(FrameProcessor):
    def __init__(
        self,
        call: CallSession,
        on_reply: Callable[[str, Any, float], None] | None = None,
        on_transfer: Callable[[str], Awaitable[bool]] | None = None,
    ) -> None:
        super().__init__()
        self.call = call
        self.on_reply = on_reply  # (text, turn, LLM+tools secs): the audio harness listens here
        # phone: hand the call (and the hand-over line) to Twilio; returns False if there's no
        # phone line to transfer on, and the agent just says the line
        self.on_transfer = on_transfer
        self.interruptions = 0
        self.dropped: dict[str, int] = {"aborted": 0, "unheard": 0}  # why replies were dropped
        self._turn = 0  # bumped on every caller interruption
        self._lock = asyncio.Lock()
        self._seen: dict[int, Any] = {}  # context messages already handed to the session
        self._tasks: set[asyncio.Task[None]] = set()
        self._closing = False
        # The latest reply until it has been heard: used to dial a transfer only after its own
        # line played, and to mark a reply the caller cut off (see _track_reply).
        self._reply_task: asyncio.Task[None] | None = None
        self._bot_speaking = False
        self._said_at = 0.0
        self._spoken: list[str] = []  # words actually spoken since the latest reply was queued
        self._reply_text = ""
        self._started_since_say = False

    async def cleanup(self) -> None:
        self._closing = True  # queued answers that get the lock now return at once
        for t in list(self._tasks):
            await self.cancel_task(t)
        await super().cleanup()

    async def _say(self, text: str) -> None:
        await self.push_frame(LLMFullResponseStartFrame())
        await self.push_frame(LLMTextFrame(text))
        await self.push_frame(LLMFullResponseEndFrame())

    def _spawn(self, coro: Any) -> None:
        self._tasks = {t for t in self._tasks if not t.done()}
        self._tasks.add(self.create_task(coro))

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, InterruptionFrame):
            self._turn += 1
            self.interruptions += 1
            self._reply_cut_off()
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
            self._started_since_say = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
        if isinstance(frame, LLMContextFrame):
            if frame.speculation:  # never act (or run booking tools) on a provisional turn
                return
            text = new_user_text(frame.context, self._seen)
            if text:
                # Not awaited here: Pipecat cancels a processor's in-flight frame when the
                # caller speaks, but the model call runs in a thread and can't be cancelled.
                # Awaiting it let a second turn start while the first was still running.
                # The turn number is taken now, so an answer that waited for the lock while
                # the caller started speaking again is recognised as stale.
                self._spawn(self._answer(text, self._turn))
            return
        await self.push_frame(frame, direction)

    def heard(self, text: str) -> None:
        """A word or phrase the caller has just heard (from SpokenWatcher)."""
        self._spoken += _words(text)

    def _reply_progress(self, text: str) -> tuple[bool, bool]:
        """(any of this reply spoken yet, its last words spoken). Matched on the reply's own
        words, so the filler's audio can't pass for it (review 2, #1)."""
        want = _words(text)
        if not want:
            return False, True
        got = self._spoken

        def has(seq: list[str]) -> bool:
            return any(got[i : i + len(seq)] == seq for i in range(len(got) - len(seq) + 1))

        # word runs, not single words: "I" or "to" in the filler mustn't count as the reply
        return has(want[:2]), has(want[-3:])

    def _reply_cut_off(self) -> None:
        """The caller spoke while the latest reply was still pending or playing."""
        task, self._reply_task = self._reply_task, None
        if task is None or task.done():
            return
        task.cancel()  # and with it any transfer it would have dialled (review 2, #1)
        if self._spoken:  # word reports are the evidence (they arrive as words are spoken)
            begun, ended = self._reply_progress(self._reply_text)
        else:  # no words reported (TTS without timestamps): judge by audio starting
            begun, ended = self._started_since_say, False
        if not begun:
            self.call.unsay()  # none of it was spoken
            self.dropped["unheard"] += 1
        elif self._bot_speaking and not ended:
            self.call.mark_interrupted()  # cut off part-way
        # else: it had finished playing; nothing to correct

    async def _track_reply(self, text: str) -> None:
        """Keep the latest reply 'pending' until it has been heard, so a caller cutting in can
        be recorded correctly (partly heard / unheard). Heard = its last words were spoken
        (SpokenWatcher) and the bot is quiet; with no word reports, the time the words take
        to say plus a grace period. Only the transcript depends on this: transfers don't
        (Twilio speaks the hand-over line itself)."""
        said_at = self._said_at
        floor = len(text) / SPOKEN_CHARS_PER_SEC
        while True:
            await asyncio.sleep(0.05)
            _, ended = self._reply_progress(text)
            waited = time.monotonic() - said_at
            if not self._bot_speaking and (ended or waited >= floor + NO_WORDS_GRACE_SECS):
                break
        self._reply_task = None

    async def _answer(self, text: str, turn: int) -> None:
        async with self._lock:  # one caller turn at a time: the session isn't re-entrant
            if self._closing:
                return
            loop = asyncio.get_running_loop()
            said_filler = False

            async def filler() -> None:
                if turn == self._turn and not self._closing:  # not for a turn already stale
                    await self.push_frame(TTSSpeakFrame(FILLER))

            def on_tools() -> None:
                nonlocal said_filler
                if not said_filler:
                    said_filler = True
                    asyncio.run_coroutine_threadsafe(filler(), loop)

            self.call.on_tools = on_tools
            t0 = datetime.now(UTC)
            reply = await asyncio.to_thread(self.call.respond, text, lambda: turn != self._turn)
            secs = (datetime.now(UTC) - t0).total_seconds()
            logger.info(f"turn {turn}: {secs:.2f}s tools={reply.tools}")
            if reply.aborted or turn != self._turn:
                # The caller kept talking: the session stopped early (aborted) or finished a
                # reply they never heard (forget it). Either way their next words continue this.
                if not reply.aborted:  # an aborted turn left nothing unheard in the history
                    self.call.unsay()
                self.dropped["aborted" if reply.aborted else "unheard"] += 1
                return
            if self.on_reply is not None:
                self.on_reply(self.call.last_input or text, reply, secs)
            if reply.transferred and self.on_transfer is not None and not self._closing:
                # Twilio says the hand-over line and dials, in that order. Shielded: once the
                # request is out, a hang-up or cleanup mustn't lose whether it went through.
                if await asyncio.shield(self.on_transfer(reply.text)):
                    return
            self._said_at, self._spoken, self._reply_text = time.monotonic(), [], reply.text
            self._started_since_say = self._bot_speaking  # audio already playing runs into it
            self._reply_task = self.create_task(self._track_reply(reply.text))
            self._tasks.add(self._reply_task)
            await self._say(reply.text)
            if reply.transferred:
                logger.info(f"transfer requested: {self.call.state.transferred}")


# Fallback only, when the TTS reports no spoken words: treat a reply as heard once the time
# its words take to say (Cartesia speaks ~15 characters a second; 14 errs long) plus a grace
# period has passed and the bot is quiet.
SPOKEN_CHARS_PER_SEC = 14.0
NO_WORDS_GRACE_SECS = 3.0


# Barge-in (VOICE-016, ≤300 ms): the caller must speak this long before the agent stops.
# Pipecat's default 0.2 s measured 0.36 s end to end; 0.12 s measured 0.27-0.31 s.
VAD_START_SECS = 0.10
# When Smart Turn thinks the caller isn't finished, the turn still ends after this much
# silence. Pipecat's default is 3 s; short closers ("Thank you!", "See you then!") were
# judged unfinished and waited the full 3 s (5 of 34 turns). The stop-early restart in
# CallSession.respond recovers if a caller really was mid-thought.
SMART_TURN_MAX_SILENCE_SECS = 1.0


def user_turns() -> LLMContextAggregatorPair:
    """Turn-taking shared by the live bot and the audio harness: Silero VAD for barge-in,
    Pipecat's Smart Turn v3 model to decide when the caller has finished."""
    vad = SileroVADAnalyzer(params=VADParams(start_secs=VAD_START_SECS))
    smart_turn = LocalSmartTurnAnalyzerV3(
        params=SmartTurnParams(stop_secs=SMART_TURN_MAX_SILENCE_SECS)
    )
    turns = UserTurnStrategies(stop=[TurnAnalyzerUserTurnStopStrategy(turn_analyzer=smart_turn)])
    return LLMContextAggregatorPair(
        LLMContext(),
        user_params=LLMUserAggregatorParams(vad_analyzer=vad, user_turn_strategies=turns),
    )


def _call_session(caller_phone: str | None) -> CallSession:
    s = get_settings()
    migrate(s.database_url)
    cfg = load_agent_config(os.path.dirname(__file__))
    cal = Calendar(s.database_url, load_salon())
    return CallSession(
        cfg,
        cal,
        ModelClient(cfg.model),
        PgRunStore(s.database_url),
        call_id=f"call-{uuid4().hex[:10]}",
        caller_phone=caller_phone,
        transcripts=ConversationStore(s.database_url, "operator"),
        langfuse=make_langfuse(s),
        notifier=Notifier(cal, JobQueue(s.database_url)),
    )


async def bot(runner_args: RunnerArguments) -> None:
    s = get_settings()
    browser = lambda: TransportParams(audio_in_enabled=True, audio_out_enabled=True)  # noqa: E731
    phone = lambda: FastAPIWebsocketParams(audio_in_enabled=True, audio_out_enabled=True)  # noqa: E731
    transport = await create_transport(
        runner_args,
        {"webrtc": browser, "twilio": phone, "telnyx": phone, "plivo": phone, "exotel": phone},
    )
    if not (s.deepgram_api_key and s.cartesia_api_key):
        raise RuntimeError("voice needs DEEPGRAM_API_KEY and CARTESIA_API_KEY in .env")

    line, call_sid, caller, our_number = None, None, s.operator_demo_caller, None
    if getattr(runner_args, "transport_type", None) == "twilio":
        if not (s.twilio_account_sid and s.twilio_auth_token):
            raise RuntimeError("phone calls need TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN in .env")
        line = TwilioCalls(s.twilio_account_sid, s.twilio_auth_token.get_secret_value())
        call_sid = runner_args.call_data.call_id  # type: ignore[attr-defined]
        try:  # the caller ID is who the agent can find bookings for (VOICE-003)
            info = await asyncio.to_thread(line.details, str(call_sid))
            caller, our_number = info.get("from") or None, info.get("to")
        except Exception as exc:  # noqa: BLE001 — no caller ID: the agent asks for a number
            logger.warning(f"caller lookup failed: {exc}")
            caller = None
    call = _call_session(caller)

    async def transfer(line_text: str) -> bool:
        """VOICE-002: hand the live call to the staff number. Twilio says `line_text` and then
        dials. Returns False when there's no phone line (browser) or no number set: the agent
        then says the line itself and staff get the callback email."""
        to = s.operator_transfer_number
        if line is None or not call_sid or not to:
            logger.warning("transfer asked but no phone line or OPERATOR_TRANSFER_NUMBER set")
            return False
        if call.transfer_dialed or getattr(call, "_ended", False):
            return True  # already handed over, or the caller is gone: never dial twice

        def dial() -> None:  # runs to completion in its thread even if the caller awaits less
            line.transfer(str(call_sid), to, our_number, say=line_text)
            call.transfer_dialed = True  # recorded where it happens, not after an await

        try:
            await asyncio.to_thread(dial)
            logger.info(f"call {call.call_id} transferred to staff")
            return True
        except Exception as exc:  # noqa: BLE001 — the call stays with the agent
            logger.error(f"transfer failed: {exc}")
            return False

    stt = DeepgramSTTService(api_key=s.deepgram_api_key.get_secret_value())
    tts = CartesiaTTSService(
        api_key=s.cartesia_api_key.get_secret_value(), voice_id=s.cartesia_voice_id
    )
    agg = user_turns()
    brain = OperatorBrain(call, on_transfer=transfer)
    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            agg.user(),
            brain,
            tts,
            transport.output(),
            SpokenWatcher(brain),  # what was actually heard: transfers wait for their line
            agg.assistant(),
        ]
    )
    task = PipelineTask(pipeline, params=PipelineParams(enable_metrics=True))

    @transport.event_handler("on_client_connected")
    async def on_connected(transport: Any, client: Any) -> None:
        await task.queue_frames([TTSSpeakFrame(call.greet())])  # disclosure first (VOICE-017)

    @transport.event_handler("on_client_disconnected")
    async def on_disconnected(transport: Any, client: Any) -> None:
        # Stop the turn and the pipeline first, then decide whether it was a missed call
        # (VOICE-002: callback job + email). end() waits for an in-flight turn to finish
        # stopping, so it runs in a thread and after the cancel (review 3, #3).
        call._ended = True  # an in-flight respond() stops at its next step
        await task.cancel()
        missed = await asyncio.to_thread(call.end)
        logger.info(f"call {call.call_id} ended; transferred={call.state.transferred} {missed=}")

    await PipelineRunner(handle_sigint=runner_args.handle_sigint).run(task)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()

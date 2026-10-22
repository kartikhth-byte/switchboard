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
    BotStoppedSpeakingFrame,
    Frame,
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSSpeakFrame,
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


def new_user_text(context: LLMContext, seen: int) -> tuple[str, int]:
    """Everything the caller said since the brain last looked (messages[seen:]). One turn can
    hold several user messages (a transcript finalised before the turn started, say); reading
    only the last one dropped the start of what they said."""
    msgs = context.get_messages()
    if seen > len(msgs):  # the context was trimmed: fall back to its last user message
        seen = max(
            (i for i, m in enumerate(msgs) if isinstance(m, dict) and m.get("role") == "user"),
            default=len(msgs),
        )
    said = [_text(m) for m in msgs[seen:] if isinstance(m, dict) and m.get("role") == "user"]
    return " ".join(t for t in said if t), len(msgs)


class OperatorBrain(FrameProcessor):
    def __init__(
        self,
        call: CallSession,
        on_reply: Callable[[str, Any, float], None] | None = None,
        on_transfer: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__()
        self.call = call
        self.on_reply = on_reply  # (text, turn, LLM+tools secs): the audio harness listens here
        self.interruptions = 0
        self.dropped = 0  # replies thrown away because the caller kept talking
        self._turn = 0  # bumped on barge-in: a reply prepared for an older turn is dropped
        self._lock = asyncio.Lock()
        self._seen = 0  # context messages already handed to the session
        self._ctx: LLMContext | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self.on_transfer = on_transfer  # phone: hand the call to staff once the line is said
        self._transfer_due = False

    async def cleanup(self) -> None:
        for t in self._tasks:
            await self.cancel_task(t)
        await super().cleanup()

    async def _say(self, text: str) -> None:
        await self.push_frame(LLMFullResponseStartFrame())
        await self.push_frame(LLMTextFrame(text))
        await self.push_frame(LLMFullResponseEndFrame())

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, InterruptionFrame):
            self._turn += 1
            self.interruptions += 1
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, BotStoppedSpeakingFrame) and self._transfer_due:
            self._transfer_due = False  # "putting you through" has been heard: now dial
            if self.on_transfer is not None:
                self._tasks.add(self.create_task(self.on_transfer()))
        if isinstance(frame, LLMContextFrame):
            if frame.speculation:  # never act (or run booking tools) on a provisional turn
                return
            if frame.context is not self._ctx:  # a new context starts from its beginning
                self._ctx, self._seen = frame.context, 0
            text, self._seen = new_user_text(frame.context, self._seen)
            if text:
                # Not awaited here: Pipecat cancels a processor's in-flight frame when the
                # caller speaks, but the model call runs in a thread and can't be cancelled.
                # Awaiting it let a second turn start while the first was still running.
                # A task outlives the interruption; the lock keeps turns in order and
                # CallSession.respond's should_stop ends the stale one early.
                self._tasks.add(self.create_task(self._answer(text)))
                self._tasks = {t for t in self._tasks if not t.done()}
            return
        await self.push_frame(frame, direction)

    async def _answer(self, text: str) -> None:
        async with self._lock:  # one caller turn at a time: the session isn't re-entrant
            turn = self._turn
            loop = asyncio.get_running_loop()
            said_filler = False

            def on_tools() -> None:
                nonlocal said_filler
                if not said_filler:
                    said_filler = True
                    asyncio.run_coroutine_threadsafe(self.push_frame(TTSSpeakFrame(FILLER)), loop)

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
                self.dropped += 1
                return
            if self.on_reply is not None:
                self.on_reply(self.call.last_input or text, reply, secs)
            if reply.transferred:
                self._transfer_due = True
            await self._say(reply.text)
            if reply.transferred:
                logger.info(f"transfer requested: {self.call.state.transferred}")


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

    async def transfer() -> None:
        """VOICE-002: ring the staff number with the live call (Twilio <Dial>)."""
        to = s.operator_transfer_number
        if line is None or not call_sid or not to:
            logger.warning("transfer asked but no phone line or OPERATOR_TRANSFER_NUMBER set")
            return
        try:
            await asyncio.to_thread(line.transfer, str(call_sid), to, our_number)
            call.transfer_dialed = True
            logger.info(f"call {call.call_id} transferred to staff")
        except Exception as exc:  # noqa: BLE001 — the call stays with the agent
            logger.error(f"transfer failed: {exc}")

    stt = DeepgramSTTService(api_key=s.deepgram_api_key.get_secret_value())
    tts = CartesiaTTSService(
        api_key=s.cartesia_api_key.get_secret_value(), voice_id=s.cartesia_voice_id
    )
    agg = user_turns()
    brain = OperatorBrain(call, on_transfer=transfer)
    pipeline = Pipeline(
        [transport.input(), stt, agg.user(), brain, tts, transport.output(), agg.assistant()]
    )
    task = PipelineTask(pipeline, params=PipelineParams(enable_metrics=True))

    @transport.event_handler("on_client_connected")
    async def on_connected(transport: Any, client: Any) -> None:
        await task.queue_frames([TTSSpeakFrame(call.greet())])  # disclosure first (VOICE-017)

    @transport.event_handler("on_client_disconnected")
    async def on_disconnected(transport: Any, client: Any) -> None:
        missed = call.end()  # VOICE-002: a missed call becomes a callback job + email
        logger.info(f"call {call.call_id} ended; transferred={call.state.transferred} {missed=}")
        await task.cancel()

    await PipelineRunner(handle_sigint=runner_args.handle_sigint).run(task)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()

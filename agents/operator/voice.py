"""Operator voice pipeline (Pipecat 1.13): browser WebRTC for development, telephony (Twilio,
Telnyx, Plivo, Exotel) through the same bot via Pipecat's runner.

    transport.in → Deepgram STT → user turn aggregator (Silero VAD, barge-in)
      → OperatorBrain (our CallSession: the same code the text evals test)
      → Cartesia TTS → transport.out

The brain stands where an LLM service normally sits: it receives the aggregated user turn
(LLMContextFrame) and answers with LLMText frames. It ignores speculative contexts (Pipecat may
run inference before the turn is final), says a short filler when the turn needs tools, and
drops a reply if the caller barged in while it was being prepared.

    make voice        # then open http://localhost:7860/client
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
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


def _last_user_text(context: LLMContext) -> str:
    for m in reversed(context.get_messages()):
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str):
                return c.strip()
            if isinstance(c, list):  # content parts
                return " ".join(p.get("text", "") for p in c if isinstance(p, dict)).strip()
    return ""


class OperatorBrain(FrameProcessor):
    def __init__(self, call: CallSession) -> None:
        super().__init__()
        self.call = call
        self._turn = 0  # bumped on barge-in: a reply prepared for an older turn is dropped
        self._lock = asyncio.Lock()

    async def _say(self, text: str) -> None:
        await self.push_frame(LLMFullResponseStartFrame())
        await self.push_frame(LLMTextFrame(text))
        await self.push_frame(LLMFullResponseEndFrame())

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, InterruptionFrame):
            self._turn += 1
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, LLMContextFrame):
            if frame.speculation:  # never act (or run booking tools) on a provisional turn
                return
            text = _last_user_text(frame.context)
            if text:
                await self._answer(text)
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
            reply = await asyncio.to_thread(self.call.respond, text)
            logger.info(
                f"turn {turn}: {(datetime.now(UTC) - t0).total_seconds():.2f}s tools={reply.tools}"
            )
            if turn != self._turn:
                return  # caller spoke over us; their new turn gets the answer
            await self._say(reply.text)
            if reply.transferred:
                logger.info(f"transfer requested: {self.call.state.transferred}")


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
    params = lambda: TransportParams(audio_in_enabled=True, audio_out_enabled=True)  # noqa: E731
    transport = await create_transport(
        runner_args,
        {"webrtc": params, "twilio": params, "telnyx": params, "plivo": params, "exotel": params},
    )
    if not (s.deepgram_api_key and s.cartesia_api_key):
        raise RuntimeError("voice needs DEEPGRAM_API_KEY and CARTESIA_API_KEY in .env")
    call = _call_session(s.operator_demo_caller)  # telephony: replaced by caller ID (M4 Twilio)
    stt = DeepgramSTTService(api_key=s.deepgram_api_key.get_secret_value())
    tts = CartesiaTTSService(
        api_key=s.cartesia_api_key.get_secret_value(), voice_id=s.cartesia_voice_id
    )
    context = LLMContext()
    agg = LLMContextAggregatorPair(
        context, user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer())
    )
    brain = OperatorBrain(call)
    pipeline = Pipeline(
        [transport.input(), stt, agg.user(), brain, tts, transport.output(), agg.assistant()]
    )
    task = PipelineTask(pipeline, params=PipelineParams(enable_metrics=True))

    @transport.event_handler("on_client_connected")
    async def on_connected(transport: Any, client: Any) -> None:
        await task.queue_frames([TTSSpeakFrame(call.greet())])  # disclosure first (VOICE-017)

    @transport.event_handler("on_client_disconnected")
    async def on_disconnected(transport: Any, client: Any) -> None:
        logger.info(f"call {call.call_id} ended; transferred={call.state.transferred}")
        await task.cancel()

    await PipelineRunner(handle_sigint=runner_args.handle_sigint).run(task)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()

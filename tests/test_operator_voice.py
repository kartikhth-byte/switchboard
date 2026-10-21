"""OperatorBrain (the CallSession inside the Pipecat pipeline), run with Pipecat's test
harness and a fake session: no audio, no keys."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

pytest.importorskip("pipecat", reason="voice group not installed (uv sync)")

from pipecat.frames.frames import (
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSSpeakFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection
from pipecat.tests.utils import run_test

from agents.operator.session import FILLER, Turn
from agents.operator.voice import OperatorBrain


class FakeCall:
    def __init__(self, tools: bool = False, delay: float = 0.0) -> None:
        self.tools, self.delay, self.heard = tools, delay, []
        self.on_tools: Any = None

        class S:
            transferred = None

        self.state = S()

    def respond(self, text: str) -> Turn:
        self.heard.append(text)
        if self.tools and self.on_tools:
            self.on_tools()
        time.sleep(self.delay)
        return Turn(f"answer to: {text}", tools=["find_slots"] if self.tools else [])


def ctx(text: str) -> LLMContext:
    c = LLMContext()
    c.add_message({"role": "user", "content": text})
    return c


def texts(frames: Any) -> list[str]:
    return [f.text for f in frames if isinstance(f, (LLMTextFrame, TTSSpeakFrame))]


def test_a_user_turn_becomes_one_spoken_reply() -> None:
    call = FakeCall()
    down, _ = asyncio.run(
        run_test(
            OperatorBrain(call),  # type: ignore[arg-type]
            frames_to_send=[LLMContextFrame(ctx("What time do you close?"))],
            expected_down_frames=[LLMFullResponseStartFrame, LLMTextFrame, LLMFullResponseEndFrame],
        )
    )
    assert call.heard == ["What time do you close?"]
    assert texts(down) == ["answer to: What time do you close?"]


def test_tool_turns_say_the_filler_first() -> None:
    down, _ = asyncio.run(
        run_test(
            OperatorBrain(FakeCall(tools=True, delay=0.2)),  # type: ignore[arg-type]
            frames_to_send=[LLMContextFrame(ctx("Saturday at 4?"))],
        )
    )
    assert texts(down) == [FILLER, "answer to: Saturday at 4?"]


def test_speculative_turns_are_never_acted_on() -> None:
    call = FakeCall(tools=True)
    down, _ = asyncio.run(
        run_test(
            OperatorBrain(call),  # type: ignore[arg-type]
            frames_to_send=[LLMContextFrame(ctx("Book me fo"), speculation=True)],
        )
    )
    assert call.heard == [] and texts(down) == []


def test_reply_is_dropped_if_the_caller_barged_in() -> None:
    call = FakeCall(delay=0.5)
    brain = OperatorBrain(call)  # type: ignore[arg-type]

    async def go() -> Any:
        async def interrupt_soon() -> None:
            await asyncio.sleep(0.1)
            await brain.process_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)

        return await asyncio.gather(
            run_test(brain, frames_to_send=[LLMContextFrame(ctx("Hi, I want"))]), interrupt_soon()
        )

    (down, _), _ = asyncio.run(go())
    assert call.heard == ["Hi, I want"] and "answer to: Hi, I want" not in texts(down)

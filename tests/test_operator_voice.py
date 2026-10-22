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
from pipecat.tests.utils import SleepFrame, run_test

from agents.operator.session import FILLER, Turn
from agents.operator.voice import OperatorBrain


class FakeCall:
    def __init__(self, tools: bool = False, delay: float = 0.0) -> None:
        self.tools, self.delay, self.heard = tools, delay, []
        self.on_tools: Any = None
        self.active = self.max_active = 0

        class S:
            transferred = None

        self.state = S()

    def unsay(self) -> None:
        self.unsaid = getattr(self, "unsaid", 0) + 1

    def respond(self, text: str, should_stop: Any = None) -> Turn:
        self.heard.append(text)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if self.tools and self.on_tools:
            self.on_tools()
        time.sleep(self.delay)
        self.active -= 1
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
            frames_to_send=[LLMContextFrame(ctx("What time do you close?")), SleepFrame(0.3)],
            expected_down_frames=[LLMFullResponseStartFrame, LLMTextFrame, LLMFullResponseEndFrame],
        )
    )
    assert call.heard == ["What time do you close?"]
    assert texts(down) == ["answer to: What time do you close?"]


def test_tool_turns_say_the_filler_first() -> None:
    down, _ = asyncio.run(
        run_test(
            OperatorBrain(FakeCall(tools=True, delay=0.2)),  # type: ignore[arg-type]
            frames_to_send=[LLMContextFrame(ctx("Saturday at 4?")), SleepFrame(0.6)],
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
            run_test(brain, frames_to_send=[LLMContextFrame(ctx("Hi, I want")), SleepFrame(0.8)]),
            interrupt_soon(),
        )

    (down, _), _ = asyncio.run(go())
    assert call.heard == ["Hi, I want"] and "answer to: Hi, I want" not in texts(down)


def test_a_turn_with_several_user_messages_is_read_whole() -> None:
    from agents.operator.voice import new_user_text

    c = LLMContext()
    c.add_message({"role": "user", "content": "Do you do nails?"})
    c.add_message({"role": "user", "content": "I want a manicure on Saturday."})
    text, seen = new_user_text(c, 0)
    assert text == "Do you do nails? I want a manicure on Saturday." and seen == 2
    c.add_message({"role": "assistant", "content": "We don't, sorry."})
    c.add_message({"role": "user", "content": "Okay, thanks."})
    assert new_user_text(c, seen)[0] == "Okay, thanks."


def test_an_interruption_never_runs_two_turns_at_once() -> None:
    """Pipecat cancels the brain's in-flight frame on an interruption; the model call is in a
    thread and keeps going. The next turn must wait for it, not run beside it."""
    call = FakeCall(delay=0.4)
    brain = OperatorBrain(call)  # type: ignore[arg-type]
    c = LLMContext()
    c.add_message({"role": "user", "content": "It's Ravi."})
    first = LLMContextFrame(c)

    async def go() -> Any:
        async def second_turn() -> None:
            await asyncio.sleep(0.1)
            await brain.process_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)
            c.add_message({"role": "user", "content": "Thank you."})
            await brain.process_frame(LLMContextFrame(c), FrameDirection.DOWNSTREAM)

        return await asyncio.gather(
            run_test(brain, frames_to_send=[first, SleepFrame(1.2)]), second_turn()
        )

    asyncio.run(go())
    assert call.heard == ["It's Ravi.", "Thank you."]
    assert call.max_active == 1

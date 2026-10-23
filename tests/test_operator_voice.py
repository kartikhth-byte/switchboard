"""OperatorBrain (the CallSession inside the Pipecat pipeline), run with Pipecat's test
harness and a fake session: no audio, no keys."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

pytest.importorskip("pipecat", reason="voice group not installed (uv sync)")

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
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
    def __init__(
        self,
        tools: bool = False,
        delay: float = 0.0,
        tools_after: float = 0.0,
        transfer: bool = False,
    ) -> None:
        self.tools, self.delay, self.heard = tools, delay, []
        self.tools_after, self.transfer = tools_after, transfer
        self.on_tools: Any = None
        self.active = self.max_active = 0
        self.last_input = ""
        self.stopped: list[str] = []
        self.marks: list[str] = []

        class S:
            transferred = None

        self.state = S()

    def unsay(self) -> None:
        self.marks.append("unheard")

    def mark_interrupted(self) -> None:
        self.marks.append("interrupted")

    def respond(self, text: str, should_stop: Any = None) -> Turn:
        self.heard.append(text)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if should_stop is not None and should_stop():  # like CallSession: check first
                self.stopped.append(text)
                return Turn("", aborted=True)
            time.sleep(self.tools_after)
            if self.tools and self.on_tools:
                self.on_tools()
            time.sleep(self.delay)
            return Turn(
                f"answer to: {text}",
                tools=["find_slots"] if self.tools else [],
                transferred=self.transfer,
            )
        finally:
            self.active -= 1


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
    seen: set[int] = set()
    assert new_user_text(c, seen) == "Do you do nails? I want a manicure on Saturday."
    c.add_message({"role": "assistant", "content": "We don't, sorry."})
    c.add_message({"role": "user", "content": "Okay, thanks."})
    assert new_user_text(c, seen) == "Okay, thanks."
    assert new_user_text(c, seen) == ""  # nothing read twice
    # trimmed to the same length with one new message: identity, not position, finds it
    msgs = c.get_messages()
    c.set_messages([*msgs[1:], {"role": "user", "content": "Bye."}])
    assert new_user_text(c, seen) == "Bye."


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


def _drive(brain: OperatorBrain, steps: list[Any], tail: float = 1.0) -> list[Any]:
    """Send frames straight to the brain, in order; a float means sleep that long."""

    async def go() -> list[Any]:
        async def script() -> None:
            await asyncio.sleep(0.05)
            for st in steps:
                if isinstance(st, (int, float)):
                    await asyncio.sleep(st)
                elif callable(st):
                    await st()
                else:
                    await brain.process_frame(st, FrameDirection.DOWNSTREAM)

        (down, _), _ = await asyncio.gather(
            run_test(brain, frames_to_send=[SleepFrame(sum_sleep(steps) + tail)]), script()
        )
        return list(down)

    return asyncio.run(go())


def sum_sleep(steps: list[Any]) -> float:
    return 0.1 + sum(x for x in steps if isinstance(x, (int, float)))


def test_cleanup_never_lets_a_queued_turn_start() -> None:
    """Review 2026-10-23 #2: cancelling the running answer released the lock while its thread
    kept going; a queued answer could then start beside it during teardown."""
    call = FakeCall(delay=0.5)
    brain = OperatorBrain(call)  # type: ignore[arg-type]
    c = LLMContext()
    c.add_message({"role": "user", "content": "first"})
    first: list[Any] = []

    async def second() -> None:
        first.extend(brain._tasks)  # only the running answer exists so far
        c.add_message({"role": "user", "content": "second"})
        await brain.process_frame(LLMContextFrame(c), FrameDirection.DOWNSTREAM)

    async def teardown() -> None:
        # cleanup() cancels answers one at a time; between cancels the queued one gets the lock
        brain._closing = True
        first[0].cancel()
        await asyncio.sleep(0.1)
        for t in list(brain._tasks):
            t.cancel()

    _drive(brain, [LLMContextFrame(c), 0.1, second, 0.05, teardown], tail=0.8)
    assert call.heard == ["first"] and call.max_active == 1


def test_an_answer_queued_behind_the_caller_speaking_again_is_stale() -> None:
    """Review #3: the turn number was read after the lock, so a queued answer looked fresh."""
    call = FakeCall(delay=0.4)
    brain = OperatorBrain(call)  # type: ignore[arg-type]
    c = LLMContext()
    c.add_message({"role": "user", "content": "A"})

    async def b_then_caller_speaks() -> None:
        c.add_message({"role": "user", "content": "B"})
        await brain.process_frame(LLMContextFrame(c), FrameDirection.DOWNSTREAM)
        await brain.process_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)

    _drive(brain, [LLMContextFrame(c), 0.1, b_then_caller_speaks], tail=1.2)
    assert "B" in call.stopped  # B never answered on its own; its words are kept for C


def test_no_filler_for_a_turn_the_caller_already_interrupted() -> None:
    """Review #7: the filler was queued from the thread even after the turn went stale."""
    call = FakeCall(tools=True, tools_after=0.3, delay=0.1)
    brain = OperatorBrain(call)  # type: ignore[arg-type]
    down = _drive(brain, [LLMContextFrame(ctx("Saturday at 4?")), 0.1, InterruptionFrame()])
    assert FILLER not in texts(down)


def test_transfer_waits_for_its_own_line_not_the_fillers_stop() -> None:
    """Review #6: any BotStoppedSpeaking dialled the transfer, e.g. the filler's."""
    dialled: list[float] = []

    async def transfer() -> None:
        dialled.append(time.monotonic())

    call = FakeCall(transfer=True)
    brain = OperatorBrain(call, on_transfer=transfer)  # type: ignore[arg-type]
    marks: dict[str, float] = {}

    async def line_ends() -> None:
        marks["end"] = time.monotonic()
        await brain.process_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)

    _drive(
        brain,
        [
            LLMContextFrame(ctx("Can I speak to a person?")),
            0.2,
            BotStoppedSpeakingFrame(),  # a late stop from earlier audio: not the line
            0.2,
            BotStartedSpeakingFrame(),  # the transfer line starts
            0.5,
            line_ends,
        ],
        tail=1.0,
    )
    assert len(dialled) == 1 and dialled[0] >= marks["end"]


def test_a_reply_cut_off_while_playing_is_marked_and_one_never_played_is_forgotten() -> None:
    """Review #5: the stored transcript said an unheard or cut-off reply was spoken."""
    call = FakeCall()
    brain = OperatorBrain(call)  # type: ignore[arg-type]
    _drive(
        brain,
        [LLMContextFrame(ctx("hi")), 0.2, BotStartedSpeakingFrame(), 0.1, InterruptionFrame()],
    )
    assert call.marks == ["interrupted"]
    call2 = FakeCall()
    brain2 = OperatorBrain(call2)  # type: ignore[arg-type]
    _drive(brain2, [LLMContextFrame(ctx("hi")), 0.2, InterruptionFrame()])
    assert call2.marks == ["unheard"] and brain2.dropped["unheard"] == 1

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
    seen: dict[int, Any] = {}
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


def test_on_the_phone_twilio_says_the_hand_over_line_and_dials() -> None:
    """Review 3 #1/#2/#6: the bot can't prove from its own audio events that a phone caller
    heard "putting you through". Twilio now says that line and then dials, in that order, so
    the bot doesn't speak it and doesn't time the dial."""
    handed: list[str] = []

    async def transfer(line: str) -> bool:
        handed.append(line)
        return True

    brain = OperatorBrain(FakeCall(transfer=True), on_transfer=transfer)  # type: ignore[arg-type]
    down = _drive(brain, [LLMContextFrame(ctx("Can I speak to a person?"))])
    assert handed == ["answer to: Can I speak to a person?"]
    assert "answer to: Can I speak to a person?" not in texts(down)  # Twilio says it


def test_without_a_phone_line_the_agent_says_the_line_itself() -> None:
    async def no_line(line: str) -> bool:
        return False

    brain = OperatorBrain(FakeCall(transfer=True), on_transfer=no_line)  # type: ignore[arg-type]
    down = _drive(brain, [LLMContextFrame(ctx("Can I speak to a person?"))])
    assert "answer to: Can I speak to a person?" in texts(down)


def test_a_hang_up_during_the_transfer_request_doesnt_lose_it() -> None:
    """Review 3 #2: cancelling the answer while the Twilio request was in flight lost whether
    it went through. The request is shielded and records its own result."""
    done: list[bool] = []

    async def slow_transfer(line: str) -> bool:
        await asyncio.sleep(0.3)
        done.append(True)
        return True

    brain = OperatorBrain(FakeCall(transfer=True), on_transfer=slow_transfer)  # type: ignore[arg-type]

    async def hang_up() -> None:
        for t in list(brain._tasks):
            t.cancel()

    _drive(brain, [LLMContextFrame(ctx("person please")), 0.1, hang_up], tail=0.6)
    assert done == [True]


def test_a_reply_cut_off_while_playing_is_marked_and_one_never_played_is_forgotten() -> None:
    """Review #5: the stored transcript said an unheard or cut-off reply was spoken. Review 2:
    the filler playing must not count as the reply being heard."""
    call = FakeCall()
    brain = OperatorBrain(call)  # type: ignore[arg-type]

    async def reply_words() -> None:
        brain.heard("answer to")

    _drive(
        brain,
        [LLMContextFrame(ctx("hi")), 0.2, BotStartedSpeakingFrame(), reply_words, 0.1,
         InterruptionFrame()],
    )  # fmt: skip
    assert call.marks == ["interrupted"]
    call2 = FakeCall()
    brain2 = OperatorBrain(call2)  # type: ignore[arg-type]

    async def filler_words() -> None:
        brain2.heard("One moment while I check that.")

    _drive(
        brain2,
        [LLMContextFrame(ctx("hi")), 0.2, BotStartedSpeakingFrame(), filler_words, 0.1,
         InterruptionFrame()],
    )  # fmt: skip
    assert call2.marks == ["unheard"] and brain2.dropped["unheard"] == 1


def test_new_user_text_survives_a_replaced_context_reusing_ids() -> None:
    """Review 2 #3: with a bare set of ids, a replaced context's message could reuse a freed
    id and the caller's next words were skipped. Holding references prevents reuse."""
    import gc

    from agents.operator.voice import new_user_text

    seen: dict[int, Any] = {}
    for i in range(50):  # many replacements: a reused id would show up as a lost turn
        c = LLMContext()
        c.add_message({"role": "user", "content": f"turn {i}"})
        assert new_user_text(c, seen) == f"turn {i}"
        del c
        gc.collect()


def test_without_word_reports_a_reply_that_played_is_not_forgotten() -> None:
    """With a TTS that reports no words, a reply that played in full and was followed by the
    caller's next turn must not be removed from history (found in a harness smoke run)."""
    call = FakeCall()
    brain = OperatorBrain(call)  # type: ignore[arg-type]
    _drive(
        brain,
        [LLMContextFrame(ctx("hi")), 0.1, BotStartedSpeakingFrame(), 0.3,
         BotStoppedSpeakingFrame(), 0.2, InterruptionFrame()],
    )  # fmt: skip
    assert call.marks == []  # heard in full: nothing to correct
    call2 = FakeCall()
    brain2 = OperatorBrain(call2)  # type: ignore[arg-type]
    _drive(brain2, [LLMContextFrame(ctx("hi")), 0.1, BotStartedSpeakingFrame(), 0.2,
                    InterruptionFrame()])  # fmt: skip
    assert call2.marks == ["interrupted"]

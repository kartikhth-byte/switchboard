"""Audio harness plumbing (VOICE-016): measurement helpers and the loopback line, no APIs."""

from __future__ import annotations

import asyncio
import math
from array import array

from pipecat.frames.frames import OutputAudioRawFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.transports.base_transport import TransportParams

from agents.operator.evals.audio import (
    FRAME,
    RATE,
    CallResult,
    LoopbackInput,
    LoopbackOutput,
    Turn,
    audio_stop_after,
    first_audio_after,
    pct,
    summarise,
    talked_over,
    voiced,
    wer,
)


def tone(secs: float, pad: float = 0.0) -> bytes:
    n, p = int(RATE * secs), int(RATE * pad)
    a = array("h", [0] * p)
    a.extend(int(8000 * math.sin(2 * math.pi * 440 * i / RATE)) for i in range(n))
    a.extend([0] * p)
    return a.tobytes()


def test_voiced_trims_padding() -> None:
    frames = voiced(tone(0.4, pad=0.3))
    assert abs(len(frames) * 0.02 - 0.4) <= 0.04
    assert voiced(b"\x00" * FRAME * 10) == []


def test_measurement_helpers() -> None:
    writes = [(0.0, 0.04), (0.04, 0.08), (0.08, 0.12), (1.0, 1.04)]
    assert first_audio_after(writes, 0.5) == 1.0
    assert first_audio_after(writes, 2.0) is None
    assert audio_stop_after(writes, 0.05) == 0.12  # the run playing at onset, not the later one
    assert talked_over(writes, 0.9, 1.1) and not talked_over(writes, 0.2, 0.9)
    assert wer("book a haircut on Saturday", "book a haircut Saturday") == 0.2
    assert pct([3.0, 1.0, 2.0], 0.5) == 2.0 and pct([], 0.5) is None


def test_summary_excludes_overlaps_and_uncounted_barge_ins() -> None:
    turns = [Turn("x", kind="plain", latency=1.0), Turn("y", kind="tool", latency=1.4)]
    ok = CallResult("a", turns=turns)
    ok.barge_in, ok.barge_counted = 0.25, True
    bad = CallResult("b", turns=[Turn("z", kind="plain", latency=0.01, overlap=True)])
    bad.barge_in, bad.barge_counted = 3.0, False  # the greeting simply ended: not a barge-in
    s = summarise([ok, bad])
    assert s["plain"] == {"n": 1, "median": 1.0, "p95": 1.0}
    assert s["barge_in"]["n"] == 1 and s["barge_in"]["max"] == 0.25
    assert s["overlap_turns"] == 1
    assert s["verdict"]["plain_median"] == "PASS" and not s["verdict"]["switch_to_vapi"]


def test_loopback_line_plays_in_real_time() -> None:
    io = TransportParams(audio_in_enabled=True, audio_out_enabled=True)
    inp, out = LoopbackInput(io), LoopbackOutput(io)
    task = PipelineTask(Pipeline([inp, out]), params=PipelineParams(audio_in_sample_rate=RATE))
    got: dict[str, float] = {}

    async def drive() -> None:
        await asyncio.sleep(0.2)
        t0, t1 = await inp.speak(tone(0.5, pad=0.2))
        got["span"] = t1 - t0
        await task.queue_frames([OutputAudioRawFrame(tone(0.3), RATE, 1)])
        await asyncio.sleep(0.8)
        await task.cancel()

    async def both() -> None:
        await asyncio.gather(PipelineRunner(handle_sigint=False).run(task), drive())

    asyncio.run(both())
    assert 0.4 <= got["span"] <= 0.6  # paced at 20 ms per frame, padding trimmed
    played = sum(e - s for s, e in out.writes)
    assert 0.2 <= played <= 0.4  # the last part-filled chunk waits for more audio
    first, last = out.writes[0][0], out.writes[-1][1]
    assert last - first >= 0.2  # the speaker took real time, not a burst

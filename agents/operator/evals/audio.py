"""Audio-mode harness (VOICE-016, plus the audio share of VOICE-017): 10 simulated callers
speak to the full voice pipeline and we time it.

    make voice-eval                       # the 10 canary calls, real Deepgram + Cartesia
    make voice-eval ARGS="--ids call-000"  # one call

The caller is the text suite's simulated caller (gpt-4o-mini, same personas) given a voice:
each line is synthesised with a different Cartesia stock voice and played into the pipeline in
real time, 20 ms frames, silence in between, exactly like a microphone. The agent's audio goes
to a loopback "speaker" that plays in real time and logs when every chunk starts and ends.

    caller TTS ─▶ LoopbackInput ─▶ Deepgram ─▶ VAD/turns ─▶ OperatorBrain ─▶ Cartesia
        ─▶ LoopbackOutput

Measured (spec §7.3):
- latency: the caller's last voiced audio frame → the agent's first audio chunk, split into
  turns without tools and turns with tools (whose first audio is the filler phrase);
- barge-in: each caller starts talking 1.2 s into the greeting; time from the caller's first
  voiced frame to the agent's audio stopping. Counted only if the brain saw the interruption;
- STT word error rate per call (the accent check), and a turn where the agent spoke over the
  caller is excluded from latency and counted.

The same code assertions as the text suite then run on the call (calendar state, canary
absent from the stored transcript), so this also gives the 10 audio calls of VOICE-017.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import time
from array import array
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from loguru import logger
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    OutputAudioRawFrame,
    StartFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import TransportParams

from agents.operator.evals import ROOT, make_env
from agents.operator.evals.target import EvalEnv, collect, prepare
from core.config.settings import get_settings
from core.evals import EvalCase, SimulatedUser, get_assertion
from core.evals.models import Persona
from core.evals.runner import load_sets
from core.evals.simuser import DONE, GIVE_UP

RATE = 16_000  # caller audio in, Hz (Deepgram's default)
FRAME = RATE // 50 * 2  # 20 ms of 16-bit mono
SILENCE = b"\x00" * FRAME
VOICED_RMS = 300  # a 20 ms frame above this is speech, below is TTS padding
BARGE_AFTER = 1.2  # s into the greeting
QUIET = 1.2  # s of agent silence that ends its turn
TURN_TIMEOUT = 45.0
# The 10 calls with a planted canary: they span create, edge, contention, reschedule, cancel
# and transfer, and VOICE-017 asks for exactly 10 audio calls checked for canaries.
CANARY_CALLS = [f"call-{i:03d}" for i in range(0, 50, 5)]
TARGETS = {"plain_median": 1.2, "tool_median": 1.5, "barge_in": 0.3, "vapi_switch_p95": 2.5}
OUT = ROOT / "evals/results/operator"


# ---------------------------------------------------------------- loopback "phone line"


class LoopbackInput(BaseInputTransport):
    """A microphone that plays queued caller audio in real time and silence otherwise."""

    def __init__(self, params: TransportParams) -> None:
        super().__init__(params)
        self._q: asyncio.Queue[tuple[bytes, asyncio.Future[float] | None]] = asyncio.Queue()
        self._mic: asyncio.Task[None] | None = None

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        await self.set_transport_ready(frame)
        if self._mic is None:
            self._mic = self.create_task(self._loop())

    async def stop(self, frame: EndFrame) -> None:
        await self._halt()
        await super().stop(frame)

    async def cancel(self, frame: CancelFrame) -> None:
        await self._halt()
        await super().cancel(frame)

    async def _halt(self) -> None:
        if self._mic is not None:
            await self.cancel_task(self._mic)
            self._mic = None

    async def _loop(self) -> None:
        nxt = time.monotonic()
        while True:
            try:
                audio, done = self._q.get_nowait()
            except asyncio.QueueEmpty:
                audio, done = SILENCE, None
            await self.push_audio_frame(InputAudioRawFrame(audio, RATE, 1))
            if done is not None and not done.done():
                done.set_result(time.monotonic())
            nxt += 0.02
            await asyncio.sleep(max(0.0, nxt - time.monotonic()))

    async def speak(self, pcm: bytes) -> tuple[float, float]:
        """Queue a clip; returns when its first and last voiced frames went out (monotonic)."""
        frames = voiced(pcm)
        if not frames:
            raise ValueError("caller clip has no voiced audio")
        loop = asyncio.get_running_loop()
        first: asyncio.Future[float] = loop.create_future()
        last: asyncio.Future[float] = loop.create_future()
        for i, f in enumerate(frames):
            mark = first if i == 0 else last if i == len(frames) - 1 else None
            await self._q.put((f, mark))
        if len(frames) == 1:
            t = await first
            return t, t
        return await first, await last


class LoopbackOutput(BaseOutputTransport):
    """A speaker: each chunk takes its own duration to 'play', and we log when."""

    def __init__(self, params: TransportParams) -> None:
        super().__init__(params)
        self.writes: list[tuple[float, float]] = []

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        await self.set_transport_ready(frame)

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        t = time.monotonic()
        dur = len(frame.audio) / (2 * frame.sample_rate * frame.num_channels)
        await asyncio.sleep(dur)
        self.writes.append((t, t + dur))
        return True


class SttTap(FrameProcessor):
    """Records every final transcript with its arrival time (STT accuracy, apart from turns)."""

    def __init__(self) -> None:
        super().__init__()
        self.finals: list[tuple[float, str]] = []

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame) and frame.text.strip():
            self.finals.append((time.monotonic(), frame.text.strip()))
        await self.push_frame(frame, direction)

    def between(self, lo: float, hi: float) -> str:
        return " ".join(t for at, t in self.finals if lo <= at <= hi)


# ---------------------------------------------------------------- pure measurement helpers


def voiced(pcm: bytes) -> list[bytes]:
    """20 ms frames with leading and trailing near-silence trimmed (TTS pads clips)."""
    frames = [pcm[i : i + FRAME].ljust(FRAME, b"\x00") for i in range(0, len(pcm), FRAME)]

    def rms(f: bytes) -> float:
        a = array("h", f)
        return (sum(v * v for v in a) / len(a)) ** 0.5 if a else 0.0

    loud = [i for i, f in enumerate(frames) if rms(f) > VOICED_RMS]
    return frames[loud[0] : loud[-1] + 1] if loud else []


def first_audio_after(writes: list[tuple[float, float]], t: float) -> float | None:
    starts = [s for s, _ in writes if s >= t]
    return min(starts) if starts else None


def audio_stop_after(writes: list[tuple[float, float]], onset: float, gap: float = 0.06) -> float:
    """End of the run of back-to-back chunks that was playing at `onset`."""
    ws = sorted(writes)
    i = next((k for k, (_, e) in enumerate(ws) if e > onset), None)
    if i is None:
        return onset
    end = ws[i][1]
    for s, e in ws[i + 1 :]:
        if s - end > gap:
            break
        end = e
    return end


def talked_over(writes: list[tuple[float, float]], lo: float, hi: float) -> bool:
    return any(lo < s < hi for s, _ in writes)


def wer(said: str, heard: str) -> float:
    a = re.findall(r"[a-z0-9']+", said.lower())
    b = re.findall(r"[a-z0-9']+", heard.lower())
    if not a:
        return 0.0
    d = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        prev, d[0] = d[0], i
        for j, y in enumerate(b, 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (x != y))
    return d[len(b)] / len(a)


def pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    k = max(0, min(len(s) - 1, round(q * (len(s) - 1))))
    return s[k]


# ---------------------------------------------------------------- one call


@dataclass
class Turn:
    said: str
    stt: str = ""  # everything Deepgram transcribed while (and just after) this line played
    got: str = ""  # what the brain answered (the part after any early turn end)
    kind: str = ""  # "plain" | "tool"
    latency: float | None = None  # caller's last voiced frame → agent's first audio
    reply_ready: float | None = None  # … → brain had the reply text (turn end + STT + LLM)
    llm: float | None = None  # model + tools time inside the brain
    split: int = 0  # replies dropped because the turn ended early and the caller went on
    overlap: bool = False
    tools: list[str] = field(default_factory=list)


@dataclass
class CallResult:
    case_id: str
    turns: list[Turn] = field(default_factory=list)
    barge_in: float | None = None
    barge_counted: bool = False
    barge_note: str = ""  # why a barge-in wasn't counted
    wer: float | None = None
    outcome: str = ""
    passed: bool = False
    metrics: dict[str, float] = field(default_factory=dict)
    cost_usd: float = 0.0
    error: str | None = None


async def synth(client: httpx.AsyncClient, key: str, voice: str, text: str) -> bytes:
    r = await client.post(
        "https://api.cartesia.ai/tts/bytes",
        headers={"X-API-Key": key, "Cartesia-Version": "2026-03-01"},
        json={
            "model_id": "sonic-3.6",
            "transcript": text,
            "voice": {"mode": "id", "id": voice},
            "output_format": {"container": "raw", "encoding": "pcm_s16le", "sample_rate": RATE},
        },
    )
    if r.status_code != 200:
        raise RuntimeError(f"Cartesia caller voice {r.status_code}: {r.text[:200]}")
    return r.content


Speech = tuple[Any, Any, Callable[[str], Awaitable[bytes]]]  # (stt, tts, caller voice)


def real_speech(http: httpx.AsyncClient) -> Speech:
    s = get_settings()
    assert s.deepgram_api_key and s.cartesia_api_key
    cart = s.cartesia_api_key.get_secret_value()
    stt = DeepgramSTTService(api_key=s.deepgram_api_key.get_secret_value())
    tts = CartesiaTTSService(api_key=cart, voice_id=s.cartesia_voice_id)
    return stt, tts, lambda text: synth(http, cart, s.operator_caller_voice_id, text)


async def run_call(env: EvalEnv, case: EvalCase, speech: Speech) -> CallResult:
    """One audio call. `speech` supplies STT, TTS and the caller's voice (fakes in tests)."""
    from agents.operator.voice import OperatorBrain, user_turns  # heavy (Silero): import late

    stt, tts, caller_voice = speech
    res = CallResult(case.id)
    p = prepare(env, case, kind="audio")
    user = SimulatedUser(Persona.model_validate(case.persona), env.sim_client)
    replies: list[tuple[float, str, Any, float]] = []  # (when, text, turn, llm secs)
    brain = OperatorBrain(
        p.call,
        on_reply=lambda text, t, secs: replies.append((time.monotonic(), text, t, secs)),
    )
    io = TransportParams(audio_in_enabled=True, audio_out_enabled=True)
    inp, out, tap = LoopbackInput(io), LoopbackOutput(io), SttTap()
    agg = user_turns()
    task = PipelineTask(
        Pipeline([inp, stt, tap, agg.user(), brain, tts, out, agg.assistant()]),
        params=PipelineParams(audio_in_sample_rate=RATE),
    )

    async def wait(cond: Any, timeout: float) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if cond():
                return True
            await asyncio.sleep(0.02)
        return False

    def quiet_since(t: float) -> bool:
        last = max((e for _, e in out.writes), default=0.0)
        return last > t and time.monotonic() - last > QUIET

    async def drive() -> None:
        greeting = p.call.greet()
        transcript: list[dict[str, str]] = []
        try:
            await asyncio.sleep(1.5)  # STT socket up
            await task.queue_frames([TTSSpeakFrame(greeting)])
            if not await wait(lambda: out.writes, 20):
                raise RuntimeError("no greeting audio")
            await asyncio.sleep(BARGE_AFTER)
            message, barge = persona_opening(case), True
            res.outcome = "max_turns"
            for _ in range(user.persona.max_turns):
                pcm = await caller_voice(message)
                n, before, drops = len(replies), brain.interruptions, brain.dropped
                t_first, t_last = await inp.speak(pcm)
                turn = Turn(said=message)
                res.turns.append(turn)
                lo = t_first
                if barge:
                    stop = audio_stop_after(out.writes, t_first)
                    playing = any(a <= t_first < b + 0.06 for a, b in out.writes)
                    interrupted = brain.interruptions > before
                    res.barge_counted = playing and interrupted
                    if not res.barge_counted:
                        res.barge_note = f"playing={playing} interrupted={interrupted}"
                    res.barge_in = stop - t_first
                    lo, barge = stop + 0.05, False
                turn.overlap = talked_over(out.writes, lo, t_last)
                if not await wait(lambda n=n: len(replies) > n, TURN_TIMEOUT):
                    res.outcome = "no_reply"
                    break
                # quiet after the reply's own audio (a tool turn has a gap after the filler)
                await wait(lambda t=t_last: quiet_since(max(t, replies[-1][0])), TURN_TIMEOUT)
                mine = replies[n:]
                turn.got = " ".join(x[1] for x in mine)
                turn.stt = tap.between(t_first, t_last + 2.0)
                turn.split = brain.dropped - drops
                turn.tools = [x for r in mine for x in r[2].tools]
                turn.kind = "tool" if turn.tools else "plain"
                turn.reply_ready = mine[0][0] - t_last
                turn.llm = mine[0][3]
                first = first_audio_after(out.writes, t_last)
                turn.latency = None if first is None else first - t_last
                agent_text = " ".join(r[2].text for r in mine)
                transcript += [
                    {"role": "user", "content": message},
                    {"role": "assistant", "content": agent_text},
                ]
                message = await asyncio.to_thread(user.reply, transcript)
                if DONE in message:
                    res.outcome = "done"
                    break
                if GIVE_UP in message:
                    res.outcome = "gave_up"
                    break
            turns_ = [r[2] for r in replies]
            ao = collect(env, p, greeting, turns_, transcript, res.outcome, user.cost_usd)
            res.cost_usd = ao.cost_usd
            for a in case.assertions:
                res.metrics[a] = float(get_assertion(a)(case, ao))
            res.passed = all(v == 1.0 for v in res.metrics.values())
            said = " ".join(t.said for t in res.turns)
            heard = " ".join(t.stt for t in res.turns)
            res.wer = round(wer(said, heard), 3) if heard else None
        except Exception as exc:  # noqa: BLE001 — a broken call is reported, the run goes on
            res.error = f"{type(exc).__name__}: {exc}"
            logger.exception(f"{case.id} failed")
        finally:
            await task.cancel()

    await asyncio.gather(PipelineRunner(handle_sigint=False).run(task), drive())
    return res


def persona_opening(case: EvalCase) -> str:
    assert case.persona is not None
    return Persona.model_validate(case.persona).opening


# ---------------------------------------------------------------- report


def summarise(results: list[CallResult]) -> dict[str, Any]:
    turns = [t for r in results for t in r.turns if t.latency is not None and not t.overlap]
    plain = [t.latency for t in turns if t.kind == "plain" and t.latency is not None]
    tool = [t.latency for t in turns if t.kind == "tool" and t.latency is not None]
    ready = [t.reply_ready for t in turns if t.kind == "plain" and t.reply_ready is not None]
    llm_plain = [t.llm for t in turns if t.kind == "plain" and t.llm is not None]
    llm_tool = [t.llm for t in turns if t.kind == "tool" and t.llm is not None]
    all_turns = [t for r in results for t in r.turns]
    barge = [r.barge_in for r in results if r.barge_counted and r.barge_in is not None]
    wers = [r.wer for r in results if r.wer is not None]
    canary = [r for r in results if "no_canary_in_transcript" in r.metrics]

    def r3(x: float | None) -> float | None:
        return None if x is None else round(x, 3)

    s: dict[str, Any] = {
        "calls": len(results),
        "errors": sum(1 for r in results if r.error),
        "plain": {"n": len(plain), "median": r3(pct(plain, 0.5)), "p95": r3(pct(plain, 0.95))},
        "tool": {"n": len(tool), "median": r3(pct(tool, 0.5)), "p95": r3(pct(tool, 0.95))},
        "plain_reply_ready_median": r3(pct(ready, 0.5)),
        "llm_median": {"plain": r3(pct(llm_plain, 0.5)), "tool": r3(pct(llm_tool, 0.5))},
        "split_turns": sum(1 for t in all_turns if t.split),
        "turns": len(all_turns),
        "barge_in": {
            "n": len(barge),
            "of": len(results),
            "median": r3(pct(barge, 0.5)),
            "max": r3(max(barge) if barge else None),
        },
        "overlap_turns": sum(1 for r in results for t in r.turns if t.overlap),
        "wer_mean": r3(statistics.mean(wers) if wers else None),
        "calls_passed": sum(1 for r in results if r.passed),
        "canary_clean": sum(1 for r in canary if r.metrics["no_canary_in_transcript"] == 1.0),
        "canary_n": len(canary),
        "cost_usd": round(sum(r.cost_usd for r in results), 4),
    }
    s["verdict"] = {
        "plain_median": _ok(s["plain"]["median"], TARGETS["plain_median"]),
        "tool_median": _ok(s["tool"]["median"], TARGETS["tool_median"]),
        "barge_in": _ok(s["barge_in"]["max"], TARGETS["barge_in"]),
        "switch_to_vapi": (s["plain"]["p95"] or 0) > TARGETS["vapi_switch_p95"],
    }
    return s


def _ok(v: float | None, target: float) -> str:
    return "not measured" if v is None else ("PASS" if v <= target else "MISS")


def to_markdown(s: dict[str, Any], voice: str, results: list[CallResult]) -> str:
    v = s["verdict"]
    lines = [
        "# Operator audio-mode run (VOICE-016, VOICE-017 audio share)",
        "",
        f"{datetime.now(UTC):%Y-%m-%d %H:%M} UTC · {s['calls']} calls · caller voice `{voice}`"
        f" · errors {s['errors']} · cost ${s['cost_usd']}",
        "",
        "| Metric | Result | Target | |",
        "|---|---|---|---|",
        f"| Non-tool turns, caller stops → agent audio | median {s['plain']['median']}s,"
        f" p95 {s['plain']['p95']}s (n={s['plain']['n']}) | median ≤1.2s | {v['plain_median']} |",
        f"| Tool turns (filler allowed) | median {s['tool']['median']}s, p95 {s['tool']['p95']}s"
        f" (n={s['tool']['n']}) | median ≤1.5s | {v['tool_median']} |",
        f"| Barge-in, caller starts → agent audio stops | median {s['barge_in']['median']}s,"
        f" max {s['barge_in']['max']}s (n={s['barge_in']['n']} of {s['barge_in']['of']})"
        f" | ≤0.3s | {v['barge_in']} |",
        f"| STT word error rate (one accent) | {s['wer_mean']} (n={s['calls']}) | reported | |",
        f"| Canary absent from stored transcript | {s['canary_clean']}/{s['canary_n']} | all | |",
        f"| Calls passing the text suite's assertions | {s['calls_passed']}/{s['calls']}"
        " | reported | |",
        "",
        f"Where the time goes, medians: non-tool reply text ready {s['plain_reply_ready_median']}s"
        f" after the caller stops, of which the model {s['llm_median']['plain']}s (the rest is"
        " turn detection and STT); then TTS to first audio. Model + tools on tool turns:"
        f" {s['llm_median']['tool']}s.",
        f"Turns ended early (caller paused, the agent's reply was dropped when they went on):"
        f" {s['split_turns']} of {s['turns']}.",
        f"Turns where the agent spoke over the caller (excluded from latency):"
        f" {s['overlap_turns']}.",
        f"Pipecat vs Vapi (switch if non-tool p95 > 2.5s): "
        f"{'SWITCH' if v['switch_to_vapi'] else 'stay on Pipecat'}.",
        "",
        "| Call | Passed | Turns | Barge-in | WER | Outcome | Error |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        b = f"{r.barge_in:.3f}" if r.barge_in is not None and r.barge_counted else "—"
        lines.append(
            f"| {r.case_id} | {'yes' if r.passed else 'no'} | {len(r.turns)} | {b} |"
            f" {r.wer} | {r.outcome} | {r.error or r.barge_note} |"
        )
    return "\n".join(lines) + "\n"


async def amain(ids: list[str]) -> dict[str, Any]:
    s = get_settings()
    if not (s.deepgram_api_key and s.cartesia_api_key):
        raise SystemExit("voice-eval needs DEEPGRAM_API_KEY and CARTESIA_API_KEY in .env")
    env = make_env()
    cases = {c.id: c for c in load_sets(ROOT / "data/fixtures/operator/evals")["calls"]}
    results: list[CallResult] = []
    async with httpx.AsyncClient(timeout=30) as http:
        for cid in ids:
            logger.info(f"audio call {cid}")
            results.append(await run_call(env, cases[cid], real_speech(http)))
    summary = summarise(results)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "audio.json").write_text(
        json.dumps({"summary": summary, "calls": [asdict(r) for r in results]}, indent=2)
    )
    (OUT / "audio_summary.md").write_text(to_markdown(summary, s.operator_caller_voice_id, results))
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", nargs="*", default=CANARY_CALLS)
    a = ap.parse_args()
    summary = asyncio.run(amain(a.ids))
    print((OUT / "audio_summary.md").read_text())
    print(json.dumps(summary["verdict"]))


if __name__ == "__main__":
    main()

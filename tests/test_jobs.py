from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from pydantic import BaseModel

from core.config.settings import get_settings
from core.jobs import Job, JobQueue, PermanentJobError, RetryPolicy, Worker
from core.tools import RetryableToolError, ToolContext, ToolRegistry, ToolSpec

# ---------- unit (no DB) ----------


def test_backoff_is_exponential_and_capped() -> None:
    p = RetryPolicy(base_s=2, max_s=30, jitter=0)
    assert [p.delay_s(n) for n in range(1, 6)] == [2, 4, 8, 16, 30]
    jittered = RetryPolicy(base_s=10, jitter=0.2).delay_s(1)
    assert 8 <= jittered <= 12


class In(BaseModel):
    x: int


def test_inline_tool_retry_with_backoff() -> None:
    sleeps: list[float] = []
    reg = ToolRegistry(sleep=sleeps.append)
    attempts = {"n": 0}

    def flaky(inp: In, ctx: ToolContext) -> dict[str, Any]:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RetryableToolError("503")
        return {"x": inp.x}

    reg.register(
        ToolSpec(name="crm", description="c", input_model=In, permission="write", side_effect=True),
        flaky,
        agents=["lead"],
    )
    ctx = ToolContext(run_id=uuid4(), agent="lead")
    r = reg.execute("crm", {"x": 1}, ctx, retry=RetryPolicy(max_attempts=5, base_s=1, jitter=0))
    assert r.ok and attempts["n"] == 3 and sleeps == [1, 2]


def test_non_retryable_tool_error_not_retried() -> None:
    reg = ToolRegistry(sleep=lambda s: None)
    calls = {"n": 0}

    def bad(inp: In, ctx: ToolContext) -> dict[str, Any]:
        calls["n"] += 1
        raise ValueError("bug")

    reg.register(
        ToolSpec(name="t", description="t", input_model=In, permission="read", side_effect=False),
        bad,
        agents=["lead"],
    )
    r = reg.execute("t", {"x": 1}, ToolContext(run_id=uuid4(), agent="lead"), retry=RetryPolicy())
    assert not r.ok and calls["n"] == 1


# ---------- integration (Postgres) ----------

pytestmark_db = pytest.mark.integration
T0 = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def queue() -> JobQueue:
    from core.db import connect, migrate

    dsn = get_settings().database_url
    migrate(dsn)
    with connect(dsn) as c:
        c.execute("DELETE FROM jobs")
    return JobQueue(dsn)


@pytestmark_db
def test_enqueue_with_key_is_idempotent(queue: JobQueue) -> None:
    a = queue.enqueue("followup", {"lead": 1}, key="lead:1:followup:1", run_at=T0)
    for _ in range(4):  # LEAD-005-style replay
        b = queue.enqueue("followup", {"lead": 1}, key="lead:1:followup:1", run_at=T0)
        assert b.id == a.id


@pytestmark_db
def test_delayed_job_runs_only_when_due(queue: JobQueue) -> None:
    queue.enqueue("reminder", key="r1", run_at=T0 + timedelta(hours=1))
    assert queue.claim(now=T0) == []
    assert [j.key for j in queue.claim(now=T0 + timedelta(hours=1))] == ["r1"]


@pytestmark_db
def test_cancel_by_key_and_prefix(queue: JobQueue) -> None:
    for i in (1, 2, 3):
        queue.enqueue("followup", key=f"lead:42:followup:{i}", run_at=T0)
    queue.enqueue("followup", key="lead:420:followup:1", run_at=T0)  # must survive prefix cancel
    assert queue.cancel("lead:42:followup:1") == 1
    assert queue.cancel_prefix("lead:42:") == 2
    assert [j.key for j in queue.claim(now=T0)] == ["lead:420:followup:1"]
    # A cancelled key can be scheduled again.
    assert queue.enqueue("followup", key="lead:42:followup:1", run_at=T0).status == "pending"


@pytestmark_db
def test_retry_with_backoff_then_failure_queue(queue: JobQueue) -> None:
    failures: list[tuple[str, str]] = []
    w = Worker(
        queue,
        policy=RetryPolicy(base_s=10, jitter=0),
        on_failure=lambda job, err: failures.append((job.kind, err)),
    )
    calls = {"n": 0}

    def flaky(job: Job) -> None:
        calls["n"] += 1
        raise TimeoutError("crm timeout")

    w.register("crm_sync", flaky)
    job = queue.enqueue("crm_sync", max_attempts=3, run_at=T0)

    assert w.run_once(now=T0) == 1  # attempt 1 fails -> retry at +10s
    got = queue.get(job.id)
    assert got is not None and got.status == "pending" and got.attempts == 1
    assert got.run_at == T0 + timedelta(seconds=10)
    assert w.run_once(now=T0 + timedelta(seconds=5)) == 0  # not due yet
    w.run_once(now=T0 + timedelta(seconds=10))  # attempt 2 -> retry at +20s
    w.run_once(now=T0 + timedelta(seconds=30))  # attempt 3 = max -> failure queue
    final = queue.get(job.id)
    assert final is not None and final.status == "failed" and calls["n"] == 3
    assert final.last_error is not None and "max_attempts" in final.last_error
    assert [j.id for j in queue.failed()] == [job.id]
    assert failures and failures[0][0] == "crm_sync"


@pytestmark_db
def test_permanent_error_and_unknown_kind_skip_retries(queue: JobQueue) -> None:
    w = Worker(queue)

    def bad(job: Job) -> None:
        raise PermanentJobError("lead opted out")

    w.register("send", bad)
    a = queue.enqueue("send", run_at=T0)
    b = queue.enqueue("mystery", run_at=T0)
    w.run_once(now=T0)
    ja, jb = queue.get(a.id), queue.get(b.id)
    assert ja is not None and ja.status == "failed" and ja.attempts == 1
    assert jb is not None and jb.last_error == "unknown_kind: mystery"


@pytestmark_db
def test_success_and_orphan_reclaim(queue: JobQueue) -> None:
    w = Worker(queue)
    done: list[str] = []
    w.register("ping", lambda job: done.append(str(job.payload["n"])))
    j = queue.enqueue("ping", {"n": 7}, run_at=T0)
    # Simulate a worker that claimed the job and crashed.
    assert len(queue.claim(now=T0)) == 1
    assert w.run_once(now=T0 + timedelta(minutes=1)) == 0  # still leased
    w.run_once(now=T0 + timedelta(minutes=6))  # lease expired -> reclaimed and run
    got = queue.get(j.id)
    assert done == ["7"] and got is not None and got.status == "done" and got.attempts == 2


@pytestmark_db
def test_concurrent_claims_never_double_take(queue: JobQueue) -> None:
    from concurrent.futures import ThreadPoolExecutor

    for i in range(30):
        queue.enqueue("ping", {"n": i}, run_at=T0)
    with ThreadPoolExecutor(6) as pool:
        batches = list(pool.map(lambda _: queue.claim(limit=5, now=T0), range(12)))
    ids = [j.id for b in batches for j in b]
    assert len(ids) == 30 and len(set(ids)) == 30

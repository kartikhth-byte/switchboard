"""Polling worker (SHR-007). Run with: python -m core.jobs

Handlers are registered per job kind by agents. Outcomes:
  return normally          -> done
  raise PermanentJobError  -> failure queue now
  raise anything else      -> retry with backoff until max_attempts, then failure queue
"""

from __future__ import annotations

import logging
import signal
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

from core.jobs.queue import Job, JobQueue
from core.jobs.retry import PermanentJobError, RetryPolicy
from core.logs import current_run_id

log = logging.getLogger(__name__)

JobHandler = Callable[[Job], None]
FailureHook = Callable[[Job, str], None]  # alerting (LEAD-005) plugs in here
POLL_S = 5.0


class Worker:
    def __init__(
        self,
        queue: JobQueue,
        *,
        policy: RetryPolicy | None = None,
        on_failure: FailureHook | None = None,
        concurrency: int = 1,
    ) -> None:
        self.queue = queue
        self.concurrency = max(1, concurrency)  # >1: a batch runs in threads (LEAD-018 burst)
        self.policy = policy or RetryPolicy()
        self.on_failure = on_failure
        self._handlers: dict[str, JobHandler] = {}
        self._stop = False

    def register(self, kind: str, handler: JobHandler) -> None:
        if kind in self._handlers:
            raise ValueError(f"handler already registered for {kind}")
        self._handlers[kind] = handler

    def run_once(self, *, now: datetime | None = None) -> int:
        """Process one batch of due jobs. Returns how many were claimed."""
        t = now or datetime.now(UTC)
        if self.concurrency == 1:
            jobs = self.queue.claim(now=now)
            for job in jobs:
                self._process(job, t)
            return len(jobs)
        jobs = self.queue.claim(limit=self.concurrency, now=now)
        with ThreadPoolExecutor(self.concurrency) as pool:
            list(pool.map(lambda j: self._process(j, t), jobs))
        return len(jobs)

    def run_forever(self, poll_s: float = POLL_S) -> None:
        signal.signal(signal.SIGTERM, self._request_stop)
        signal.signal(signal.SIGINT, self._request_stop)
        log.info("worker started", extra={"event": "worker_started"})
        while not self._stop:
            try:
                claimed = self.run_once()
            except Exception:  # DB blip: log and keep polling
                log.exception("worker poll failed")
                claimed = 0
            if not claimed:
                time.sleep(poll_s)

    def _request_stop(self, *_: Any) -> None:
        self._stop = True

    def _process(self, job: Job, now: datetime) -> None:
        token = current_run_id.set(str(job.run_id) if job.run_id else None)
        try:
            handler = self._handlers.get(job.kind)
            if handler is None:
                self._to_failure_queue(job, f"unknown_kind: {job.kind}")
                return
            try:
                handler(job)
            except PermanentJobError as exc:
                self._to_failure_queue(job, f"permanent: {type(exc).__name__}: {exc}")
                return
            except Exception as exc:  # noqa: BLE001 — every other error is retryable
                err = f"{type(exc).__name__}: {exc}"
                if job.attempts >= job.max_attempts:
                    self._to_failure_queue(job, f"max_attempts: {err}")
                else:
                    when = now + timedelta(seconds=self.policy.delay_s(job.attempts))
                    self.queue.retry_at(job.id, when, err)
                    log.warning(
                        "job retry scheduled",
                        extra={"event": "job_retry", "tool": job.kind},
                    )
                return
            self.queue.complete(job.id)
        finally:
            current_run_id.reset(token)

    def _to_failure_queue(self, job: Job, error: str) -> None:
        self.queue.fail(job.id, error)
        log.error("job failed", extra={"event": "job_failed", "tool": job.kind})
        if self.on_failure is not None:
            try:
                self.on_failure(job, error)
            except Exception:  # an alerting outage must not wedge the worker
                log.exception("failure hook raised")


def main() -> None:
    from core.config.settings import get_settings
    from core.db import migrate
    from core.logs import setup_logging

    setup_logging()
    dsn = get_settings().database_url
    migrate(dsn)
    # Agents register their job kinds here as they land (M3: leads, M4: reminders).
    from agents.dispatcher.service import Dispatcher, make_adapters
    from core.tracing import make_langfuse

    settings = get_settings()
    dispatcher = Dispatcher(settings, make_adapters(settings), langfuse=make_langfuse(settings))
    worker = Worker(
        JobQueue(dsn),
        on_failure=dispatcher.on_job_failure,
        concurrency=settings.worker_concurrency,
    )
    dispatcher.register_jobs(worker)
    worker.run_forever()


if __name__ == "__main__":
    main()

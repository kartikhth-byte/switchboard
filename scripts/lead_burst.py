"""LEAD-018 burst test: 30 leads arrive at once; p95 from intake to the first reply queued
(sent, held for outbound-off, or waiting for approval) must be <=60s.

Needs `make up` (the worker processes the jobs, with WORKER_CONCURRENCY threads) and the
Anthropic key in .env. Spends real tokens: about 2 model calls per lead, counted as live runs.

    make lead-burst            # 30 leads
    make lead-burst ARGS="--n 10"
"""

from __future__ import annotations

import argparse
import json
import math
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from agents.dispatcher.service import Dispatcher, mock_adapters
from core.config.settings import get_settings
from core.db import connect, migrate

ROOT = Path(__file__).resolve().parents[1]
READY = ("sent", "held", "approval_requested", "no_reply")


def p(values: list[float], q: float) -> float:
    s = sorted(values)
    return s[max(0, math.ceil(q * len(s)) - 1)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--timeout", type=float, default=300)
    a = ap.parse_args()
    s = get_settings()
    migrate(s.database_url)
    d = Dispatcher(s, mock_adapters(s.database_url), dedupe_scope=f"burst-{uuid4().hex[:8]}")
    lines = (ROOT / "data/fixtures/lead/evals/leads.jsonl").read_text().splitlines()
    payloads = [json.loads(x)["input"]["payload"] for x in lines if x.strip()][: a.n]
    now = datetime.now(UTC).isoformat()
    ids = [str(d.intake({**pl, "received_at": now})[0].id) for pl in payloads]
    print(f"queued {len(ids)} leads; waiting for the worker…")

    t0, done = time.monotonic(), {}
    while len(done) < len(ids) and time.monotonic() - t0 < a.timeout:
        with connect(s.database_url) as c:
            rows = c.execute(
                "SELECT l.id::text AS id, EXTRACT(EPOCH FROM min(e.at) - l.created_at) AS secs"
                " FROM leads l JOIN lead_events e ON e.lead_id = l.id"
                " WHERE l.id = ANY(%s::uuid[]) AND e.kind = ANY(%s) GROUP BY l.id",
                (ids, list(READY)),
            ).fetchall()
        done = {r["id"]: float(r["secs"]) for r in rows}
        time.sleep(1)
    secs = list(done.values())
    missing = len(ids) - len(secs)
    if not secs:
        print("no lead reached a reply: is the worker running (`make up`)?")
        return 1
    res = {
        "n": len(ids),
        "finished": len(secs),
        "p50_s": round(p(secs, 0.5), 1),
        "p95_s": round(p(secs, 0.95), 1),
        "max_s": round(max(secs), 1),
        "concurrency": s.worker_concurrency,
        "target_p95_s": 60,
    }
    ok = missing == 0 and res["p95_s"] <= 60
    out = ROOT / "evals/results/dispatcher/burst.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "# Dispatcher burst test (LEAD-018)\n\n"
        f"{datetime.now(UTC):%Y-%m-%d %H:%M} UTC · worker concurrency {res['concurrency']}\n\n"
        "| n | finished | p50 | p95 | max | target |\n|---|---|---|---|---|---|\n"
        f"| {res['n']} | {res['finished']} | {res['p50_s']}s | {res['p95_s']}s | {res['max_s']}s"
        f" | p95 ≤60s: {'PASS' if ok else 'FAIL'} |\n"
    )
    print(json.dumps(res), "PASS" if ok else "FAIL", "->", out.relative_to(ROOT))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

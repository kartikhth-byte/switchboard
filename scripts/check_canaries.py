"""SHR-019 acceptance: planted PII canaries must not appear in exported traces.

Scans the Langfuse traces of the long-conversation eval sessions (where a canary phone, card or
spoken number is planted in turn 1 and carried in memory for 29 more turns), including every
observation's input and output. Exit 0 = no canary found in >= 100 traces.

  make canary-check            # after any eval run that included long_conversation
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from core.config.settings import get_settings

CANARIES = [  # must match data/generators/support_evals.py
    "+91 98450 12345",
    "+1 (555) 013-7788",
    "9845012345",
    "4111 1111 1111 1111",
    "5555-5555-5555-4444",
    "nine eight four five zero one two three four five",
]
SESSIONS = [f"eval-long-{i:03d}" for i in range(1, 11)]
TARGET = 100
DAYS = 7  # look back this far for eval traces


def _filters(session: str) -> list[list[dict[str, str]]]:
    """Trace sessionId (runs since session propagation), then the root observation's
    metadata.session_id (older runs recorded it only there)."""
    return [
        [{"type": "string", "column": "sessionId", "operator": "=", "value": session}],
        [{"type": "stringObject", "column": "metadata", "key": "session_id", "operator": "=",
          "value": session}],
    ]  # fmt: skip


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def find_canaries(text: str) -> list[str]:
    """Exact match, or the canary's digits appearing inside a run of digits in the text (catches
    re-spaced or re-punctuated copies such as 98450-12345)."""
    found = [c for c in CANARIES if c.lower() in text.lower()]
    digit_runs = [_digits(m) for m in re.findall(r"[\d][\d\s().+-]{6,}[\d]", text)]
    for c in CANARIES:
        d = _digits(c)
        if d and c not in found and any(d in run for run in digit_runs):
            found.append(c)
    return found


def main() -> int:
    s = get_settings()
    if not (s.langfuse_public_key and s.langfuse_secret_key):
        print("Langfuse keys not set — can't check exported traces.")
        return 1
    auth = (s.langfuse_public_key.get_secret_value(), s.langfuse_secret_key.get_secret_value())
    url = f"{s.langfuse_host}/api/public/v2/observations"  # v1 /traces is gone (410)
    now = datetime.now(UTC)
    by_trace: dict[str, list[dict[str, Any]]] = defaultdict(list)
    with httpx.Client(auth=auth, timeout=60) as http:
        for session, filt in [(sess, f) for sess in SESSIONS for f in _filters(sess)]:
            if any(r.get("_session") == session for rows in by_trace.values() for r in rows):
                continue  # already found via the first filter
            cursor: str | None = None
            while True:
                params: dict[str, Any] = {
                    "fields": "core,basic,io,trace_context",
                    "filter": json.dumps(filt),
                    "fromStartTime": (now - timedelta(days=DAYS)).isoformat(),
                    "toStartTime": now.isoformat(),
                    "limit": 1000,
                }  # fmt: skip
                if cursor:
                    params["cursor"] = cursor
                r = http.get(url, params=params)
                if r.status_code != 200:
                    print(
                        f"Langfuse API error {r.status_code} ({filt[0]['column']}): {r.text[:200]}"
                    )
                    break
                body = r.json()
                for row in body.get("data", []):
                    by_trace[row["traceId"]].append({**row, "_session": session})
                cursor = (body.get("meta") or {}).get("cursor")
                if not cursor:
                    break
    scanned, leaks, planted = len(by_trace), [], 0
    for tid, rows in by_trace.items():
        text = json.dumps(rows, ensure_ascii=False)
        planted += "You can reach me on" in text  # the turn that carries the canary (control)
        hits = find_canaries(text)
        if hits:
            leaks.append((rows[0]["_session"], tid, hits))
    if planted == 0 and scanned:
        print("no trace contains the planted turn: input/output not returned, so nothing proven")
        return 1
    print(f"scanned {scanned} traces from {len(SESSIONS)} sessions; "
          f"{planted} contain the planted-canary turn")  # fmt: skip
    for session, tid, hits in leaks:
        print(f"  LEAK {session} trace {tid}: {hits}")
    if scanned < TARGET:
        print(f"only {scanned} traces found (need {TARGET}): run the long_conversation set first")
        return 1
    print("no canaries found" if not leaks else f"{len(leaks)} traces leak canaries")
    return 0 if not leaks else 2


if __name__ == "__main__":
    sys.exit(main())

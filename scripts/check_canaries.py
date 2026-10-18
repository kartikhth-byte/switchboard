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
    base = f"{s.langfuse_host}/api/public"
    scanned, leaks = 0, []
    with httpx.Client(auth=auth, timeout=30) as http:
        for session in SESSIONS:
            r = http.get(f"{base}/traces", params={"sessionId": session, "limit": 50})
            r.raise_for_status()
            for t in r.json().get("data", []):
                detail = http.get(f"{base}/traces/{t['id']}")
                detail.raise_for_status()
                body: dict[str, Any] = detail.json()
                hits = find_canaries(json.dumps(body, ensure_ascii=False))
                scanned += 1
                if hits:
                    leaks.append((session, t["id"], hits))
    print(f"scanned {scanned} traces (with observations) from {len(SESSIONS)} sessions")
    for session, tid, hits in leaks:
        print(f"  LEAK {session} trace {tid}: {hits}")
    if scanned < TARGET:
        print(f"only {scanned} traces found (need {TARGET}): run the long_conversation set first")
        return 1
    print("no canaries found" if not leaks else f"{len(leaks)} traces leak canaries")
    return 0 if not leaks else 2


if __name__ == "__main__":
    sys.exit(main())

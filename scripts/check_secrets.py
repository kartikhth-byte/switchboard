"""LEAD-016 / SHR-005: no secrets in exported traces or service logs.

Looks for (a) the exact values of every key configured in .env and (b) common token shapes
(Anthropic, OpenAI, Langfuse, Slack, GoHighLevel, Meta, AWS, private keys) in:
  - Langfuse observations of the last 7 days (inputs, outputs, metadata), and
  - service logs piped on stdin.

    make secrets-scan     # = docker compose logs --no-color api worker | ... --logs -
Exit 0 = nothing found in >= 30 traces.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from core.config.settings import PROVIDER_KEYS, get_settings

PATTERNS = {
    "anthropic": r"sk-ant-[A-Za-z0-9_-]{20,}",
    "openai": r"sk-(?:proj-)?[A-Za-z0-9_-]{32,}",
    "langfuse": r"(?:sk|pk)-lf-[0-9a-f-]{20,}",
    "slack": r"xox[abposr]-[0-9A-Za-z-]{10,}",
    "ghl_pit": r"pit-[0-9a-f-]{20,}",
    "meta": r"EAA[A-Za-z0-9]{40,}",
    "aws": r"AKIA[0-9A-Z]{16}",
    "private_key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
}
MIN_TRACES = 30


def configured_secrets() -> dict[str, str]:
    import os

    s = get_settings()
    vals = {k: os.environ.get(k, "") for k in PROVIDER_KEYS}
    for name in type(s).model_fields:
        v = getattr(s, name)
        if hasattr(v, "get_secret_value"):
            vals[name.upper()] = v.get_secret_value()
    return {k: v for k, v in vals.items() if v and len(v) >= 12}


def scan(text: str, secrets: dict[str, str]) -> list[str]:
    hits = [f"value of {k}" for k, v in secrets.items() if v in text]
    hits += [f"{name} pattern" for name, rx in PATTERNS.items() if re.search(rx, text)]
    return hits


def traces(days: int = 7) -> dict[str, str]:
    s = get_settings()
    if not (s.langfuse_public_key and s.langfuse_secret_key):
        return {}
    auth = (s.langfuse_public_key.get_secret_value(), s.langfuse_secret_key.get_secret_value())
    now, out, cursor = datetime.now(UTC), {}, None  # type: ignore[var-annotated]
    with httpx.Client(auth=auth, timeout=60) as http:
        for _ in range(10):  # up to 10k observations
            params: dict[str, Any] = {
                "fields": "core,basic,io,metadata",
                "fromStartTime": (now - timedelta(days=days)).isoformat(),
                "toStartTime": now.isoformat(),
                "limit": 1000,
            }
            if cursor:
                params["cursor"] = cursor
            r = http.get(f"{s.langfuse_host}/api/public/v2/observations", params=params)
            if r.status_code != 200:
                print(f"Langfuse API error {r.status_code}: {r.text[:200]}")
                break
            body = r.json()
            for row in body.get("data", []):
                out[row["traceId"]] = out.get(row["traceId"], "") + json.dumps(row)
            cursor = (body.get("meta") or {}).get("cursor")
            if not cursor:
                break
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--logs", help="log file to scan, or - for stdin")
    a = ap.parse_args()
    secrets = configured_secrets()
    found = 0
    by_trace = traces()
    for tid, text in by_trace.items():
        if hits := scan(text, secrets):
            found += 1
            print(f"  LEAK trace {tid}: {hits}")
    lines = 0
    if a.logs:
        stream = sys.stdin if a.logs == "-" else open(a.logs, encoding="utf-8")  # noqa: SIM115
        for i, line in enumerate(stream, 1):
            lines = i
            if hits := scan(line, secrets):
                found += 1
                print(f"  LEAK log line {i}: {hits}")
    print(
        f"checked {len(secrets)} configured secrets + {len(PATTERNS)} token patterns in "
        f"{len(by_trace)} traces and {lines} log lines: {found} leaks"
    )
    if len(by_trace) < MIN_TRACES:
        print(f"only {len(by_trace)} traces in the last 7 days (need {MIN_TRACES})")
        return 1
    return 0 if found == 0 else 2


if __name__ == "__main__":
    sys.exit(main())

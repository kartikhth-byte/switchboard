"""Verify Langfuse credentials from env without printing them. Exit 0 = OK.

Usage: uv run python scripts/check_langfuse.py
"""

import sys

import httpx

from core.config.settings import get_settings


def main() -> int:
    s = get_settings()
    if not (s.langfuse_public_key and s.langfuse_secret_key):
        print("Langfuse keys not set (LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY) — skipped.")
        return 1
    auth = (s.langfuse_public_key.get_secret_value(), s.langfuse_secret_key.get_secret_value())
    try:
        r = httpx.get(f"{s.langfuse_host}/api/public/projects", auth=auth, timeout=10)
    except httpx.HTTPError as exc:
        print(f"Langfuse unreachable at {s.langfuse_host}: {type(exc).__name__}")
        return 1
    if r.status_code != 200:
        print(f"Langfuse rejected the keys: HTTP {r.status_code}")
        return 1
    names = [p.get("name") for p in r.json().get("data", [])]
    print(f"Langfuse OK at {s.langfuse_host} — project(s): {names}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

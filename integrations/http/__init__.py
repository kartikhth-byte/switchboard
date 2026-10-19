"""Shared bits for the real HTTP adapters (demo runs only, DEMO_INTEGRATIONS=real)."""

from __future__ import annotations

import httpx

TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class IntegrationError(RuntimeError):
    """A provider rejected the call. The message never contains tokens or full payloads."""


def check(resp: httpx.Response, provider: str) -> dict:  # type: ignore[type-arg]
    if resp.status_code >= 400:
        detail = resp.text[:200].replace("\n", " ")
        raise IntegrationError(f"{provider} HTTP {resp.status_code}: {detail}")
    data: dict = resp.json() if resp.content else {}  # type: ignore[type-arg]
    return data

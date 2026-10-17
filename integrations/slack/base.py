"""Slack connector interface (SHR-012). Used for human handoff (SUPP-010) and hot leads (M3)."""

from __future__ import annotations

from typing import Any, Protocol


class Slack(Protocol):
    def post(self, channel: str, text: str, payload: dict[str, Any] | None = None) -> str: ...

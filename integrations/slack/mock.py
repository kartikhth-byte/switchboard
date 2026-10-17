"""Records messages instead of sending them (CI default; real Slack is demo-only, M3)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class MockSlack:
    messages: list[dict[str, Any]] = field(default_factory=list)

    def post(self, channel: str, text: str, payload: dict[str, Any] | None = None) -> str:
        ts = f"mock-{len(self.messages) + 1}"
        self.messages.append({"channel": channel, "text": text, "payload": payload or {}, "ts": ts})
        return ts

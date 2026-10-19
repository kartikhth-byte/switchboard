"""Slack Web API chat.postMessage (demo runs). Used by the Dispatcher (hot leads, #ops-alerts)
and the Concierge handoff. Channel names map to IDs via SLACK_CHANNELS ("#leads=C0123,...")."""

from __future__ import annotations

import json
from typing import Any

import httpx

from integrations.http import TIMEOUT, IntegrationError, check

API = "https://slack.com/api/chat.postMessage"


class SlackWeb:
    def __init__(
        self, token: str, channels: dict[str, str] | None = None, client: httpx.Client | None = None
    ) -> None:
        self._headers = {"Authorization": f"Bearer {token}"}
        self.channels = channels or {}
        self.http = client or httpx.Client(timeout=TIMEOUT)

    def post(self, channel: str, text: str, payload: dict[str, Any] | None = None) -> str:
        blocks: list[dict[str, Any]] = [
            {"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}}
        ]
        p = payload or {}
        extras = [f"*{k}:* {v}" for k, v in p.items() if k in ("owner", "tier", "crm_url") and v]
        if extras:
            blocks.append(
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": " · ".join(extras)[:2900]}],
                }
            )
        if p.get("thread"):
            thread = "\n".join(f"> *{m.get('from')}*: {m.get('text')}" for m in p["thread"])
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": thread[:2900]}})
        body = {"channel": self.channels.get(channel, channel), "text": text, "blocks": blocks}
        data = check(
            self.http.post(API, headers=self._headers, content=json.dumps(body), timeout=TIMEOUT),
            "slack",
        )
        if not data.get("ok"):  # Slack returns 200 with ok=false on errors
            raise IntegrationError(f"slack: {data.get('error', 'unknown_error')}")
        return str(data["ts"])


def parse_channels(spec: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in spec.split(","):
        if "=" in part:
            name, cid = part.split("=", 1)
            out[name.strip()] = cid.strip()
    return out

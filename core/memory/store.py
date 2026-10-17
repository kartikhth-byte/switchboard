"""Conversation state persistence (SHR-013). Agents store their own state as JSON."""

from __future__ import annotations

import json
from typing import Any

from core.db import connect


class ConversationStore:
    def __init__(self, dsn: str, agent: str) -> None:
        self.dsn = dsn
        self.agent = agent

    def load(self, conversation_id: str) -> dict[str, Any] | None:
        with connect(self.dsn) as c:
            row = c.execute(
                "SELECT state FROM conversations WHERE id = %s AND agent = %s",
                (conversation_id, self.agent),
            ).fetchone()
        return dict(row["state"]) if row and isinstance(row["state"], dict) else None

    def save(self, conversation_id: str, state: dict[str, Any]) -> None:
        with connect(self.dsn) as c:
            c.execute(
                "INSERT INTO conversations (id, agent, state) VALUES (%s, %s, %s)"
                " ON CONFLICT (id) DO UPDATE SET state = EXCLUDED.state, updated_at = now()",
                (conversation_id, self.agent, json.dumps(state)),
            )

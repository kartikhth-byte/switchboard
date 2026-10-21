"""Calendar mirror (VOICE-003 demo runs): Postgres is the calendar of record; Google Calendar
gets a copy. Event ids are the appointment's UUID hex (valid Google event ids), so a retried
sync updates the same event instead of creating a second one."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from core.config.settings import Settings

log = logging.getLogger(__name__)


class CalendarMirror(Protocol):
    def upsert(
        self, event_id: str, summary: str, start: datetime, end: datetime, description: str
    ) -> str: ...
    def delete(self, event_id: str) -> None: ...


@dataclass
class MockCalendarMirror:
    events: dict[str, dict[str, Any]] = field(default_factory=dict)

    def upsert(
        self, event_id: str, summary: str, start: datetime, end: datetime, description: str
    ) -> str:
        self.events[event_id] = {
            "summary": summary,
            "start": start,
            "end": end,
            "description": description,
        }
        return event_id

    def delete(self, event_id: str) -> None:
        self.events.pop(event_id, None)


def make_calendar_mirror(settings: Settings) -> CalendarMirror:
    if (
        settings.demo_integrations == "real"
        and settings.google_service_account_file
        and settings.google_calendar_id
    ):
        from integrations.calendar.google import GoogleCalendarMirror

        return GoogleCalendarMirror(
            settings.google_service_account_file, settings.google_calendar_id
        )
    if settings.demo_integrations == "real":
        log.warning("Google Calendar not configured: the calendar mirror is the mock")
    return MockCalendarMirror()


__all__ = ["CalendarMirror", "MockCalendarMirror", "make_calendar_mirror"]

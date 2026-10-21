"""Google Calendar via a service account (demo runs). The test calendar must be shared with the
service account's email ("Make changes to events"). Times are sent with their UTC offset."""

from __future__ import annotations

from datetime import datetime
from urllib.parse import quote

import httpx
from google.auth.transport.requests import Request
from google.oauth2 import service_account

from integrations.http import TIMEOUT, IntegrationError, check

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
API = "https://www.googleapis.com/calendar/v3/calendars"


class GoogleCalendarMirror:
    def __init__(self, key_file: str, calendar_id: str, client: httpx.Client | None = None) -> None:
        self.creds = service_account.Credentials.from_service_account_file(  # type: ignore[no-untyped-call]
            key_file, scopes=SCOPES
        )
        self.base = f"{API}/{quote(calendar_id, safe='')}/events"
        self.http = client or httpx.Client(timeout=TIMEOUT)

    def _headers(self) -> dict[str, str]:
        if not self.creds.valid:
            self.creds.refresh(Request())
        return {"Authorization": f"Bearer {self.creds.token}"}

    def upsert(
        self, event_id: str, summary: str, start: datetime, end: datetime, description: str
    ) -> str:
        body = {
            "id": event_id,
            "summary": summary,
            "description": description,
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
        }
        r = self.http.post(self.base, headers=self._headers(), json=body)
        if r.status_code == 409:  # already mirrored (retry, or a reschedule restored it): update
            r = self.http.put(
                f"{self.base}/{event_id}",
                headers=self._headers(),
                json={**body, "status": "confirmed"},
            )
        return str(check(r, "google_calendar")["id"])

    def delete(self, event_id: str) -> None:
        r = self.http.delete(f"{self.base}/{event_id}", headers=self._headers())
        if r.status_code not in (200, 204, 404, 410):  # gone already is fine
            raise IntegrationError(f"google_calendar HTTP {r.status_code}: {r.text[:200]}")

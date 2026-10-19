"""GoHighLevel (LeadConnector API v2) contact upsert for demo runs (LEAD-001, LEAD-005).

Auth is a Private Integration token for one sub-account (location). GoHighLevel's upsert
matches an existing contact by email/phone per the location's duplicate settings, so a
replayed lead updates the same contact instead of creating a second one.
"""

from __future__ import annotations

from typing import Any

import httpx

from integrations.http import TIMEOUT, check

BASE = "https://services.leadconnectorhq.com"
VERSION = "2021-07-28"


class GoHighLevelCRM:
    def __init__(self, token: str, location_id: str, client: httpx.Client | None = None) -> None:
        self.location_id = location_id
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Version": VERSION,
            "Accept": "application/json",
        }
        self.http = client or httpx.Client(base_url=BASE, timeout=TIMEOUT)

    def upsert_lead(self, email: str | None, phone: str | None, fields: dict[str, Any]) -> str:
        name = str(fields.get("name") or "").strip()
        first, _, last = name.partition(" ")
        tags = [t for t in ("switchboard", fields.get("channel"), fields.get("campaign")) if t]
        body: dict[str, Any] = {
            "locationId": self.location_id,
            "firstName": first or None,
            "lastName": last or None,
            "email": email,
            "phone": phone,
            "source": fields.get("source") or "switchboard",
            "tags": tags,
        }
        body = {k: v for k, v in body.items() if v not in (None, "", [])}
        data = check(self.http.post("/contacts/upsert", headers=self._headers, json=body), "ghl")
        return str(data["contact"]["id"])

    def contact_url(self, crm_id: str) -> str:
        return (
            f"https://app.gohighlevel.com/v2/location/{self.location_id}/contacts/detail/{crm_id}"
        )

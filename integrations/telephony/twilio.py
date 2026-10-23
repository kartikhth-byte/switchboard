"""Twilio REST for a live call: read the caller's number, and transfer the call to staff by
replacing its TwiML with <Say> (the hand-over line) then <Dial>. That ends the media stream (so
the bot hangs up) and Twilio rings the staff line, falling back to a short message if nobody
answers."""

from __future__ import annotations

from typing import Any
from xml.sax.saxutils import escape

import httpx

from integrations.http import TIMEOUT, check

API = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls/{call}.json"


def transfer_twiml(to: str, caller_id: str | None, say: str = "", timeout_s: int = 20) -> str:
    """Twilio speaks the hand-over line itself, then dials: Twilio guarantees the caller hears
    the line before the ringing, which the bot can't prove from its own audio events."""
    cid = f' callerId="{escape(caller_id)}"' if caller_id else ""
    lead = f"<Say>{escape(say)}</Say>" if say else ""
    return (
        f"<Response>{lead}"
        f'<Dial timeout="{timeout_s}"{cid}>{escape(to)}</Dial>'
        "<Say>Sorry, nobody could take the call. The salon will call you back.</Say>"
        "</Response>"
    )


class TwilioCalls:
    def __init__(self, account_sid: str, auth_token: str, client: httpx.Client | None = None):
        self.sid = account_sid
        self.http = client or httpx.Client(timeout=TIMEOUT, auth=(account_sid, auth_token))

    def details(self, call_sid: str) -> dict[str, Any]:
        """The call's numbers: {"from": caller, "to": our Twilio number, ...}."""
        return check(self.http.get(API.format(sid=self.sid, call=call_sid)), "twilio")

    def transfer(self, call_sid: str, to: str, caller_id: str | None = None, say: str = "") -> str:
        data = check(
            self.http.post(
                API.format(sid=self.sid, call=call_sid),
                data={"Twiml": transfer_twiml(to, caller_id, say)},
            ),
            "twilio",
        )
        return str(data.get("status", ""))

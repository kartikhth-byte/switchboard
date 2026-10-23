"""Twilio call control (VOICE-001/002): caller lookup and live transfer, against fake HTTP."""

from __future__ import annotations

from urllib.parse import parse_qs

import httpx

from integrations.telephony import TwilioCalls, transfer_twiml


def test_transfer_twiml_says_the_line_then_dials_then_apologises() -> None:
    x = transfer_twiml("+919800000099", "+17370000000", say="Putting you through now.")
    assert x.index("<Say>Putting you through now.</Say>") < x.index("<Dial")
    assert '<Dial timeout="20" callerId="+17370000000">+919800000099</Dial>' in x
    assert x.index("</Dial>") < x.index("<Say>Sorry")  # the apology plays only if nobody answers
    assert "&lt;" in transfer_twiml("<x>", None)  # escaped


def test_details_and_transfer_hit_the_live_call() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.method == "GET":
            return httpx.Response(200, json={"from": "+919800000042", "to": "+17370000000"})
        return httpx.Response(200, json={"status": "in-progress"})

    line = TwilioCalls("AC123", "tok", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert line.details("CA1")["from"] == "+919800000042"
    assert line.transfer("CA1", "+919800000099", "+17370000000") == "in-progress"
    assert seen[0].url.path == "/2010-04-01/Accounts/AC123/Calls/CA1.json"
    form = parse_qs(seen[1].content.decode())
    assert "<Dial" in form["Twiml"][0] and "+919800000099" in form["Twiml"][0]

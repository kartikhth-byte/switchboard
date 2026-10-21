"""LEAD-007: the exported n8n workflow is well-formed, holds no secrets, and its mapping node
turns the form fixture into a valid intake request. (The live import/run is `make n8n-e2e`.)"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WF = json.loads((ROOT / "n8n/lead_intake.json").read_text())
NODES = {n["name"]: n for n in WF["nodes"]}


def test_chain_webhook_map_post_reply() -> None:
    chain, name = [], "Form webhook"
    while name:
        chain.append(NODES[name]["type"].split(".")[-1])
        nxt = WF["connections"].get(name, {}).get("main", [[]])[0]
        name = nxt[0]["node"] if nxt else None
    assert chain == ["webhook", "code", "httpRequest", "respondToWebhook"]
    post = NODES["POST /agents/lead/intake"]["parameters"]
    assert post["method"] == "POST" and post["url"].endswith("/agents/lead/intake")


def test_no_credentials_or_secrets_in_export() -> None:
    text = json.dumps(WF)
    assert not any("credentials" in n for n in WF["nodes"])
    assert not re.search(r"(sk-|xoxb-|pit-|Bearer\s+\w)", text)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_mapping_node_output_is_a_valid_intake_request() -> None:
    from api.main import LeadIn

    form = json.loads((ROOT / "data/fixtures/lead/n8n_form.json").read_text())
    js = NODES["Map to lead schema"]["parameters"]["jsCode"]
    harness = (
        f"const $input = {{ all: () => [{{ json: {{ body: {json.dumps(form)} }} }}] }};\n"
        f"const run = () => {{ {js} }};\nconsole.log(JSON.stringify(run()));"
    )
    out = json.loads(
        subprocess.run(  # noqa: S603  (our own workflow file, test-only)
            [shutil.which("node") or "node", "-e", harness],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    lead = LeadIn.model_validate(out[0]["json"])
    assert lead.channel == "web_form" and lead.name == "Meera Iyer"
    assert lead.utm == {
        "utm_source": "instagram",
        "utm_medium": "paid_social",
        "utm_campaign": "festive_2026",
    }
    assert lead.campaign == "festive_2026" and "Casa Kokum" in lead.message

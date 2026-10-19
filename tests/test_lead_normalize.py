"""LEAD-010: 100% correct on 50 fixture variants (expected values written by hand)."""

import json
from pathlib import Path

import pytest

from agents.dispatcher.normalize import dedupe_key, normalize, normalize_email, normalize_phone

FIXTURE = Path(__file__).parents[1] / "data/fixtures/lead/normalization.jsonl"
ROWS = [json.loads(line) for line in FIXTURE.read_text().splitlines()]


def test_fixture_has_50_variants() -> None:
    assert len(ROWS) == 50


@pytest.mark.parametrize("row", ROWS, ids=[r["id"] for r in ROWS])
def test_variant(row: dict[str, object]) -> None:
    fn = normalize_phone if row["field"] == "phone" else normalize_email
    value, issue = fn(str(row["raw"]))
    assert value == row["expected"]
    if row["expected"] is None and str(row["raw"]).strip():
        assert issue == f"invalid_{row['field']}"  # flagged, not silently dropped


def test_dedupe_key_prefers_email_then_phone() -> None:
    a = normalize(" Asha@Example.org ", "098765 43210")
    b = normalize("asha@example.org", None)
    assert dedupe_key(a) == dedupe_key(b) == "email:asha@example.org"
    assert dedupe_key(normalize(None, "+91 98765 43210")) == "phone:+919876543210"
    assert dedupe_key(normalize("bad@", "12")) is None
    assert normalize("bad@", "12").issues == ["invalid_email", "invalid_phone"]

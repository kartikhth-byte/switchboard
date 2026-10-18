"""Calibration export takes each question from the run itself, never from the current dataset."""

import csv
import json
from pathlib import Path

import pytest

from scripts import judge_calibration as jc


def test_export_uses_run_transcript_and_keeps_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jc, "CSV_PATH", tmp_path / "c.csv")
    monkeypatch.setattr(jc, "SIDECAR", tmp_path / "c.json")
    case = {
        "case_id": "ookb-g001",  # in today's dataset this id is a different question
        "metrics": {"no_invented_policy": 1.0},
        "answer": "I don't have tailor recommendations.",
        "notes": {
            "judge": {"invented_policy": {"score": 1, "reason": "x"}},
            "retrieved": {"d#0": {"text": "kb text"}},
            "transcript": [{"role": "user", "content": "Any tailor in Mapusa?"},
                           {"role": "assistant", "content": "I don't have..."}],
        },
    }  # fmt: skip
    run = tmp_path / "run.json"
    run.write_text(json.dumps({"cases": [case]}))
    jc.export(run)
    rows = list(csv.DictReader(jc.CSV_PATH.open()))
    assert rows[0]["question"] == "Any tailor in Mapusa?"
    rows[0]["human_label"] = "1"
    with jc.CSV_PATH.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    jc.export(run)  # re-export keeps the label for the same question + answer
    assert next(csv.DictReader(jc.CSV_PATH.open()))["human_label"] == "1"

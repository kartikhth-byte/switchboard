"""Judge calibration (spec §8: >=85% agreement with 30 hand labels before judge metrics count).

  make calib-export RESULTS=evals/results/concierge/<run>.json   # picks 30 judged answers
  (fill human_label = 1 or 0 in data/fixtures/support/judge_calibration.csv)
  make calibrate                                                 # re-judges them, reports agreement

The labels are about the answer, not about the judge, so they stay valid when the judge model
or its inputs change: rerun `make calibrate` after any judge change.
"""

from __future__ import annotations

import csv
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CSV_PATH = ROOT / "data/fixtures/support/judge_calibration.csv"
SIDECAR = ROOT / "data/fixtures/support/judge_calibration.json"  # full judge inputs per item
RUBRIC_OF = {"grounded": "groundedness", "no_invented_policy": "invented_policy"}
PER_RUBRIC = 15
sys.path.insert(0, str(ROOT))
from data.generators.support_evals import OUT_OF_KB_REJECTED  # noqa: E402

TARGET = 0.85


def _h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def export(results: Path) -> None:
    run = json.loads(results.read_text(encoding="utf-8"))
    old = _labels()  # keep labels already given to an unchanged question + answer
    items: list[dict[str, Any]] = []
    for metric, rubric in RUBRIC_OF.items():
        judged = [
            c
            for c in run["cases"]
            if metric in c["metrics"]
            and c["notes"].get("judge")
            and _question(c) not in OUT_OF_KB_REJECTED
        ]  # mislabelled since the run
        # Every judge "fail" (the judge is most often wrong there), then passes, deterministic.
        fails = [c for c in judged if c["metrics"][metric] == 0.0]
        passes = sorted((c for c in judged if c["metrics"][metric] == 1.0),
                        key=lambda c: _h(c["case_id"]))  # fmt: skip
        picked = (fails + passes)[:PER_RUBRIC]
        for c in sorted(picked, key=lambda c: _h(rubric + c["case_id"])):
            n = c["notes"]
            retrieved = n.get("retrieved") or {}
            items.append({
                "item": f"{rubric[:3]}-{len(items) + 1:02d}",
                "case_id": c["case_id"],
                "rubric": rubric,
                "metric": metric,
                # From the run's own transcript, never the current dataset: case ids can point
                # at different questions after `make data` (that bug mismatched 4 rows once).
                "question": _question(c),
                "answer": c["answer"] or "",
                "kb_excerpts": "\n\n".join(f"[{k}] {retrieved[k]['text']}" for k in retrieved),
                "data": {"retrieved": retrieved, "citations": n.get("citations") or [],
                         "escalated": n.get("escalated", False)},
            })  # fmt: skip
    with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["item", "rubric", "human_label", "note", "question", "answer", "kb_excerpts"])
        kept = 0
        for it in items:
            label, note = old.get((it["question"], it["answer"]), ("", ""))
            kept += bool(label)
            w.writerow([it["item"], it["rubric"], label, note, it["question"], it["answer"],
                        it["kb_excerpts"]])  # fmt: skip
    print(f"kept {kept} existing labels")
    SIDECAR.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {len(items)} items to {CSV_PATH.name} (fill human_label 1/0)")


def _question(c: dict[str, Any]) -> str:
    return str(next(t["content"] for t in c["notes"]["transcript"] if t["role"] == "user"))


def _labels() -> dict[tuple[str, str], tuple[str, str]]:
    if not CSV_PATH.exists():
        return {}
    with CSV_PATH.open(encoding="utf-8") as f:
        return {(r["question"], r["answer"]): (r["human_label"].strip(), r["note"])
                for r in csv.DictReader(f) if r["human_label"].strip()}  # fmt: skip


def score() -> int:
    from agents.concierge.evals import JUDGE, assertions
    from core.config.settings import export_provider_keys
    from core.evals import AgentOutput, EvalCase, Judge, agreement
    from core.models import ModelClient

    export_provider_keys()
    items = {it["item"]: it for it in json.loads(SIDECAR.read_text(encoding="utf-8"))}
    with CSV_PATH.open(encoding="utf-8") as f:
        labels = {r["item"]: r["human_label"].strip() for r in csv.DictReader(f)}
    missing = [k for k, v in labels.items() if v not in ("0", "1")]
    if missing:
        print(f"{len(missing)} items have no 1/0 human_label yet: {', '.join(missing[:5])}…")
        return 1
    judge = Judge(ModelClient(JUDGE))
    assertions.set_judge(judge)
    rows: list[tuple[str, str, int, int]] = []
    for key, it in items.items():
        case = EvalCase(id=it["case_id"], agent="concierge", input={"message": it["question"]},
                        assertions=[it["metric"]], expected={})  # fmt: skip
        out = AgentOutput(answer=it["answer"], data=dict(it["data"]))
        verdict = int(getattr(assertions, it["metric"])(case, out))
        rows.append((key, it["rubric"], verdict, int(labels[key])))
    for rubric in RUBRIC_OF.values():
        sub = [r for r in rows if r[1] == rubric]
        print(f"{rubric}: agreement {agreement([r[2] for r in sub], [r[3] for r in sub]):.1%}"
              f" (n={len(sub)})")  # fmt: skip
    total = agreement([r[2] for r in rows], [r[3] for r in rows])
    print(
        f"overall: {total:.1%} (n={len(rows)}), target {TARGET:.0%}, judge {JUDGE.name}, "
        f"cost ${judge.cost_usd:.3f}"
    )
    for key, rubric, j, h in rows:
        if j != h:
            print(f"  disagree {key} ({rubric}): judge {j}, human {h}")
    return 0 if total >= TARGET else 2


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "export":
        export(Path(sys.argv[2]))
        sys.exit(0)
    sys.exit(score())

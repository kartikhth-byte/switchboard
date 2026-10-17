"""Shared eval runner (SHR-009). `make eval AGENT=<name> [ARGS="--smoke"]`.

Soundness rules from spec §8, enforced here rather than by convention:
- every reported metric carries its n;
- sets with >=30 cases get a deterministic dev (~70%) / test (~30%) split and the
  default report is the test split; smaller sets are pass/fail with no split;
- results are broken down by tag (happy_path / edge / adversarial) and by set;
- judge and simulated user must use a different provider than the agent;
- a cost cap stops the suite and marks the report truncated;
- --repeat N reports mean and min across runs (variance run, once before README).
"""

from __future__ import annotations

import hashlib
import importlib
import json
import subprocess  # noqa: S404 — git sha for the report
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from typing import Any, Literal

from core.evals.assertions import get_assertion
from core.evals.models import AgentOutput, EvalCase, EvalResult
from core.evals.simuser import check_cross_provider
from core.models import ModelClient

Split = Literal["test", "dev", "all"]
SPLIT_MIN_CASES = 30
DEV_SHARE = 7  # out of 10
SMOKE_N = 20


@dataclass
class EvalSuite:
    agent: str
    cases_dir: Path  # one <set>.jsonl per eval set
    target: Callable[[EvalCase], AgentOutput]
    agent_provider: str
    helpers: list[ModelClient] = field(default_factory=list)  # judge, simulated user


def load_sets(cases_dir: Path) -> dict[str, list[EvalCase]]:
    sets: dict[str, list[EvalCase]] = {}
    for path in sorted(cases_dir.glob("*.jsonl")):
        cases = [
            EvalCase.model_validate_json(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        ids = [c.id for c in cases]
        if len(ids) != len(set(ids)):
            raise ValueError(f"{path}: duplicate case ids")
        sets[path.stem] = cases
    if not sets:
        raise ValueError(f"no *.jsonl eval sets in {cases_dir}")
    return sets


def _bucket(case_id: str) -> int:
    return int(hashlib.sha256(case_id.encode()).hexdigest(), 16) % 10


def split_of(case_id: str, set_size: int) -> str:
    if set_size < SPLIT_MIN_CASES:
        return "all"
    return "dev" if _bucket(case_id) < DEV_SHARE else "test"


def select(
    sets: dict[str, list[EvalCase]], split: Split, smoke: bool
) -> list[tuple[str, EvalCase, str]]:
    chosen: list[tuple[str, EvalCase, str]] = []
    for name, cases in sets.items():
        for c in cases:
            s = split_of(c.id, len(cases))
            if split == "all" or s in (split, "all"):
                chosen.append((name, c, s))
    if smoke:  # deterministic, spread across sets
        chosen = sorted(chosen, key=lambda t: hashlib.sha256(t[1].id.encode()).hexdigest())
        chosen = chosen[:SMOKE_N]
    return chosen


def run_case(suite: EvalSuite, case: EvalCase, split: str) -> EvalResult:
    try:
        out = suite.target(case)
    except Exception as exc:  # noqa: BLE001 — a crashing case is a failed case, not a crashed suite
        return EvalResult(
            case_id=case.id,
            passed=False,
            metrics={a: 0.0 for a in case.assertions},
            trace_id="",
            cost_usd=0.0,
            tags=case.tags,
            split=split,
            error=type(exc).__name__,
        )
    metrics: dict[str, float] = {}
    errors: list[str] = []
    for a in case.assertions:
        try:
            metrics[a] = float(get_assertion(a)(case, out))
        except Exception as exc:  # noqa: BLE001 — e.g. judge API down: fail the metric, keep going
            metrics[a] = 0.0
            errors.append(f"{a}: {type(exc).__name__}")
    notes: dict[str, Any] = {
        k: out.data[k] for k in ("judge", "citations", "escalated", "status") if k in out.data
    }
    return EvalResult(
        case_id=case.id,
        passed=all(v == 1.0 for v in metrics.values()),
        metrics=metrics,
        trace_id=out.trace_id,
        # Assertions may spend too (LLM judge); they record it in data["helper_cost_usd"].
        cost_usd=out.cost_usd + float(out.data.get("helper_cost_usd", 0.0)),
        tags=case.tags,
        split=split,
        error="; ".join(errors) or None,
        answer=out.answer,
        notes=notes,
    )


def _summarise(results: list[EvalResult]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {
        "pass_rate": {"value": mean(r.passed for r in results), "n": len(results)}
    }
    per: dict[str, list[float]] = defaultdict(list)
    for r in results:
        for k, v in r.metrics.items():
            per[k].append(v)
    for k, vals in sorted(per.items()):
        out[k] = {"value": mean(vals), "n": len(vals)}
    return out


def run_suite(
    suite: EvalSuite,
    *,
    split: Split = "test",
    smoke: bool = False,
    repeat: int = 1,
    max_usd: float | None = None,
    out_dir: Path = Path("evals/results"),
) -> dict[str, Any]:
    check_cross_provider(suite.agent_provider, *suite.helpers)
    sets = load_sets(suite.cases_dir)
    chosen = select(sets, split, smoke)
    runs: list[list[EvalResult]] = []
    by_set: dict[str, str] = {c.id: name for name, c, _ in chosen}
    spent, truncated = 0.0, False
    for _ in range(repeat):
        this_run: list[EvalResult] = []
        for _name, case, s in chosen:
            if max_usd is not None and spent >= max_usd:
                truncated = True
                break
            res = run_case(suite, case, s)
            spent += res.cost_usd
            this_run.append(res)
        runs.append(this_run)
        if truncated:
            break

    results = runs[0]
    report: dict[str, Any] = {
        "agent": suite.agent,
        "created_at": datetime.now(UTC).isoformat(),
        "git_sha": _git_sha(),
        "split": split,
        "splits_used": sorted({s for _, _, s in chosen}),  # "all" = set <30, pass/fail, no split
        "smoke": smoke,
        "repeat": len(runs),
        "truncated_by_cost_cap": truncated,
        "cost_usd": round(spent, 6),
        "sets": {name: len(cases) for name, cases in sets.items()},
        "metrics": _summarise(results) if results else {},
        "by_tag": {},
        "by_set": {},
        "cases": [r.model_dump() for r in results],
    }
    tags = sorted({t for r in results for t in r.tags})
    for t in tags:
        report["by_tag"][t] = _summarise([r for r in results if t in r.tags])
    for name in sets:
        sub = [r for r in results if by_set.get(r.case_id) == name]
        if sub:
            report["by_set"][name] = _summarise(sub)
    if len(runs) > 1 and all(runs):
        rates = [mean(r.passed for r in run) for run in runs]
        report["variance"] = {"pass_rate_mean": mean(rates), "pass_rate_min": min(rates)}

    _write(report, out_dir)
    return report


def to_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"### {report['agent']} — split `{report['split']}`"
        + (" (smoke)" if report["smoke"] else ""),
        "",
        "Sets under 30 cases are pass/fail tests with no split."
        if "all" in report["splits_used"]
        else "Reported on the held-out test split.",
        "",
        "| Metric | Value | n |",
        "|---|---:|---:|",
    ]
    for k, m in report["metrics"].items():
        lines.append(f"| {k} | {m['value']:.1%} | {m['n']} |")
    if report["by_tag"]:
        lines += ["", "| Category | Pass rate | n |", "|---|---:|---:|"]
        for t, m in report["by_tag"].items():
            lines.append(f"| {t} | {m['pass_rate']['value']:.1%} | {m['pass_rate']['n']} |")
    if "variance" in report:
        v = report["variance"]
        lines.append(
            f"\nPass rate over {report['repeat']} runs: mean {v['pass_rate_mean']:.1%},"
            f" min {v['pass_rate_min']:.1%}"
        )
    capped = " — stopped by cost cap" if report["truncated_by_cost_cap"] else ""
    lines.append(f"\nCost: ${report['cost_usd']:.4f}{capped}")
    return "\n".join(lines) + "\n"


def _write(report: dict[str, Any], out_dir: Path) -> None:
    d = out_dir / report["agent"]
    d.mkdir(parents=True, exist_ok=True)
    stem = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ") + ("-smoke" if report["smoke"] else "")
    (d / f"{stem}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (d / f"{stem}.md").write_text(to_markdown(report), encoding="utf-8")
    report["paths"] = [str(d / f"{stem}.json"), str(d / f"{stem}.md")]


def _git_sha() -> str | None:
    try:
        out = subprocess.run(  # noqa: S603 — fixed command
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.strip() or None


def load_suite(agent: str) -> EvalSuite:
    """`echo` is the built-in trivial suite; real agents expose agents/<name>/evals.py:suite()."""
    module = "core.evals.examples.echo" if agent == "echo" else f"agents.{agent}.evals"
    mod = importlib.import_module(module)
    suite: EvalSuite = mod.suite()
    return suite

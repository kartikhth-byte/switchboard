"""python -m core.evals <agent> [--smoke] [--split test|dev|all] [--repeat N] [--max-usd X]"""

import argparse
import sys

from core.evals.runner import Split, load_suite, run_suite, to_markdown


def main() -> int:
    p = argparse.ArgumentParser(prog="make eval AGENT=<name>")
    p.add_argument("agent")
    p.add_argument("--smoke", action="store_true", help="deterministic ~20-case subset (CI)")
    p.add_argument(
        "--split",
        choices=["test", "dev", "all"],
        default=None,
        help="default: dev for --smoke (tuning), test otherwise (reporting)",
    )
    p.add_argument("--repeat", type=int, default=1, help="variance run: report mean and min")
    p.add_argument("--max-usd", type=float, default=None, help="stop when spend reaches this")
    a = p.parse_args()
    split: Split = a.split or ("dev" if a.smoke else "test")
    report = run_suite(
        load_suite(a.agent), split=split, smoke=a.smoke, repeat=a.repeat, max_usd=a.max_usd
    )
    print(to_markdown(report))
    print("written:", *report["paths"])
    return 0


if __name__ == "__main__":
    sys.exit(main())

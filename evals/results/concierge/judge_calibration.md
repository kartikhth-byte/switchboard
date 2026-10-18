# Concierge judge calibration (spec §8.4)

Judge: OpenAI gpt-4o (agent runs on Claude). 30 answers from the dev run of 2026-10-18,
hand-labelled by the owner (`data/fixtures/support/judge_calibration.csv`). Run 2026-10-18,
`make calibrate`, cost $0.086.

| Rubric | Agreement | n |
|---|---:|---:|
| groundedness | 80.0% | 15 |
| invented_policy | 93.3% | 15 |
| **overall** | **86.7%** | **30** |

Target: ≥85% on 30 labels — **met**. Disagreements (4) run both ways, so no systematic bias
to fix in the rubric:

| Item | Judge | Human | Note |
|---|---:|---:|---|
| gro-09 | 0 | 1 | judge too strict |
| gro-11 | 1 | 0 | judge missed "laptops" added to the safe guidance |
| gro-12 | 0 | 1 | judge too strict |
| inv-29 | 1 | 0 | judge missed an implied stay-extension process |

Caveat reported with the numbers: per-rubric n is 15, so groundedness agreement has a wide
interval (one item = 6.7 points). Re-run `make calibrate` after any judge or rubric change.

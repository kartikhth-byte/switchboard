# Dispatcher eval summary (M3)

Test split, commit `772796f`, 2026-10-19, Claude Sonnet via LiteLLM, cost $0.30.
All assertions are code (no judge). The prompt was tuned only on dev-split failures
(s025, h009, s043); the test split was run once after that.

| Requirement | Metric | Target | Result | n |
|---|---|---|---:|---:|
| LEAD-002 | Intent correct | ≥85% | 94.1% | 17 |
| LEAD-002 | Tier exact | ≥80% | 94.1% | 17 |
| LEAD-002 | Tier within one | ≥95% | 100% | 17 |
| LEAD-013 / 020 | 0 messages after reply / takeover / opt-out (follow-ups forced to fire) | 0 | 0 in 20 | 20 |
| LEAD-005 | Replay 30 leads × 5 concurrent deliveries → one lead, one contact, one reply each | 0 duplicates | 0 | 150 |

n=17 is small: one case is ~6 points, so read the LEAD-002 numbers as "above target", not as
precise rates. The one miss was a hand-written web-form lead (intent and tier off by one).

Dev split (smoke, n=16) for reference: 87.5% / 81.2% / 93.8% before the rubric fix,
93.8% / 93.8% / 100% after — optimistic, since those cases drove the fix.

Not yet measured: LEAD-018 burst (`make lead-burst`, p95 ≤60s).

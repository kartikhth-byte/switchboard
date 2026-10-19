# Dispatcher eval summary (M3)

Test split, commit `772796f`, 2026-10-19, claude-sonnet-4-5 via LiteLLM, cost $0.30.
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

| LEAD-018 | 30-lead burst, intake → first reply queued (real model, 8 worker threads) | p95 ≤60s | p95 35.4s (p50 17.2s, max 37.0s), 30/30 finished | 30 |

The first burst run failed (23/30 finished): concurrent checkpointer setup crashed 7 first
attempts, and their retries had no checkpoint to resume. The same investigation found the
per-lead claim's row lock was a no-op under autocommit. Both fixed in `dae7a58`; the run above
is after the fix.

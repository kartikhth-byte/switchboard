# Operator eval summary (M4, text mode)

Test split, commit `345ff36`, 2026-10-21, claude-haiku-4-5 via LiteLLM; simulated caller
gpt-4o-mini. Cost $0.46. All assertions are code; the Postgres calendar is the ground truth.
Prompt fixes were made on dev-split failures only (call-012, call-020, faq-009); the test split
was run once after that.

| Requirement | Metric | Target | Result | n |
|---|---|---|---:|---:|
| VOICE-001 | FAQ answered correctly (facts present; unknowns offered a callback/transfer) | ≥95% | 100% | 7 |
| VOICE-003 | Booking task success (DB state matches the caller's goal) | ≥90% | **88.2%** (miss) | 17 |
| VOICE-003 | Success claimed only when the DB backs it | 100% | **94.1%** (1 claim) | 17 |
| VOICE-002 | Transfer when asked/complaint; none on ordinary bookings | 100% | 94.1% | 17 |
| VOICE-017 | Opening line discloses AI + recording | 100% | 100% | 17 |
| VOICE-017 | Planted canary absent from stored transcripts | 0 | 0 | 17 (3 planted) |
| VOICE-004 | Racing callers, 20 runs (unit test) | 0 double bookings | 0 | 20 |

**The two failures (both reschedules):**
- **call-032: label error.** "Move my Sunday appointment to Saturday instead": the agent booked
  the Saturday just before, which is the natural reading; the generator had labelled the
  following Saturday. Label fixed in `baf243d`. By the transcript this case would pass (16/17 =
  94.1%), but that is a reading, not a re-run, so the reported number stays 88.2%. It is also
  the one "false success" above: the booking it claimed exists, just on the date the label
  didn't expect.
- **call-028: model variance.** The agent transferred to staff on its first reply to a plain
  reschedule. Re-running the same case (`--ids call-028`) passed cleanly (find booking → slot →
  hold with replaces → confirm). Tool calls weren't recorded at the time, so the trigger is
  unknown; they are now (`tool_log` in the results).

The planned 3× variance run on the test split before the README (spec §8) will report mean and
min for these, with the corrected label.

Dev split (smoke, n=20): 85% → 100% after the fixes (optimistic: those cases drove the fixes).
Not yet measured: audio-mode latency and barge-in (VOICE-016), confirmations and reminders
(VOICE-005), the Google Calendar mirror (VOICE-003 demo runs), missed-call callbacks (VOICE-002).

# Data card: Operator eval sets

- **Source:** hand-designed scenarios in `data/generators/operator_evals.py` (no model generation);
  the fictional Studio Nine Salon in `agents/operator/config/`. No real people or numbers.
- **calls.jsonl (50):** 22 bookings (incl. a stylist without the skill, a closed day, a late
  start that just fits, a change of mind), 4 slot-already-taken, 10 reschedules, 6 cancellations,
  4 transfers (complaint, asks for a manager, 2 services the salon doesn't offer), 4 after-hours
  calls. 10 calls plant the canary number `+91 98450 12345` (VOICE-017).
- **faq.jsonl (30):** 24 answerable from the salon profile/config with the facts a correct answer
  must contain (digits or spoken forms), 3 the salon doesn't offer, 3 the profile doesn't cover
  (expect an offer of a callback or transfer).
- **slots.jsonl (8, added 2026-10-23):** questions only the whole day's availability answers,
  phrased so the time resolver doesn't catch them ("how late", "after work", "is 3:15 free?" after
  a list). Men's haircuts with Farhan (the only stylist for it), seeded bookings, so each case has
  exactly one right start time. 2 more (women's haircut, blow-dry) share the service across
  stylists, so the day's first/last is across everyone. Built to compare `find_slots` with and without its count and
  per-day first/last (`OPERATOR_SLOT_SUMMARY=off`). Weeks 100-105, clear of the other sets.
- **times_grounded (all calls and slots, added 2026-10-23):** every clock time the agent says
  is checked by code against a snapshot of the calendar taken before the call (Postgres, the
  case's service): offers must be real free starts or bookings, "latest/earliest" must be the
  day's real last/first, "taken/isn't free" must not be free. Calibrated on 63 stored calls
  (201 sentences with times): it flagged 4 claims, all real agent errors; 30 passed claims
  hand-checked, all correct. Limits: one service per call; the day comes from the sentence
  (weekday, ordinal, "tomorrow") or the case's date; a time without am/pm passes if either
  reading is right.
- **Labels:** expected DB end state per call (service, stylist if specified, date, start-time
  window; seeded booking cancelled); facts per FAQ. Checked by code against Postgres.
- **Isolation:** each call runs in its own calendar week from 2027-01-05; that week is reset
  before the call, so re-runs are reproducible.
- **Splits:** calls and faq ≥30 → dev ~70% / test ~30% by the runner. Tune only on dev.
  slots (6) is a pass/fail set with no split: report it with its n, don't tune on it.
- **Licence:** project code licence (MIT).

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
- **Labels:** expected DB end state per call (service, stylist if specified, date, start-time
  window; seeded booking cancelled); facts per FAQ. Checked by code against Postgres.
- **Isolation:** each call runs in its own calendar week from 2027-01-05; that week is reset
  before the call, so re-runs are reproducible.
- **Splits:** both sets ≥30 → dev ~70% / test ~30% by the runner. Tune only on dev.
- **Licence:** project code licence (MIT).

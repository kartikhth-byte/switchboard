# Data card: Dispatcher eval sets

**Location:** `data/fixtures/lead/` · **Generator:** `data/generators/lead_evals.py`
(`make data-lead`, seed 11, labelling date fixed at 2026-10-17) · **Licence:** repository.

| Set | n | How it was made | Labels | Requirement |
|---|---:|---|---|---|
| `evals/leads` | 60 | 45 synthetic from a persona × channel × intent grid, then paraphrase-perturbed by `gpt-4o-mini` (a paraphrase that drops any number is discarded); 15 rewritten from anonymised real enquiries (`handwritten.csv`) | intent (8 classes), tier 1–4 by the rubric in `agents/dispatcher/config/labels.md` — applied in code to the grid's slots, by hand for the 15 real ones; hand-checked | LEAD-002, LEAD-003, LEAD-001 (payload attribution) |
| `evals/takeover` | 20 | Template: a warm lead (follow-ups scheduled), then a reply, a human takeover or an opt-out phrase 1–30 h later | expected messages after the event: 0 | LEAD-013, LEAD-020 |
| `normalization.jsonl` | 50 | Hand-written phone and email variants (Indian, US, UK, UAE, DE, AU formats; typical form typos) | expected E.164 / normalised email written by hand, not by the library under test | LEAD-010 |

Payloads carry `channel`, `source`, `campaign`, `utm_*`, `received_at`, and name/email/phone from a
fictional pool (`example.org`, `+91 98765 4xxxx` test-style numbers). Replay, dedupe and burst
tests reuse these payloads (LEAD-005, LEAD-018).

**Real enquiries (OQ-5):** 15 first messages from the owner's Airbnb export were rewritten in
Palmgrove Stays terms: names, the real listings, landmarks, dates and phone numbers removed or
changed; style kept. Only the messages file was read; the temporary extract was deleted.

**Known limits:** the synthetic grid's wording is narrow before paraphrasing; capacity, festive
minimum-stay and pet rules are kept out of the synthetic slots so labels depend only on the rubric.

# Phase 2 backlog (logged during Wave 1, not built)

Ideas raised while building Wave 1. Wave 1 scope is frozen; each row gets copied into the
traceability matrix as a Phase 2 requirement with these acceptance criteria.

| ID (proposed) | Idea | Why | Accept |
|---|---|---|---|
| SUPP-P2-01 | TypeSafe Jev for the Concierge escalation check, A/B against gpt-4o-mini | Built for single-choice classification; ~5× cheaper; confidence score allows gating | Same or better escalation recall and false-escalation rate on the dev escalation set incl. Hinglish cases; calls traced, costed and budget-capped via `run.model` |
| SUPP-P2-02 | Offer a person before handing off (3-way: escalate / offer / answer) | Fin's pattern for anger and vague "agent?" requests; cuts needless handoffs | False escalation lower than the 2-way check on the same conversations, recall unchanged |
| SUPP-P2-03 | Loop detection: same question asked 2+ times → handoff | Built-in trigger in Fin; catches stuck guests the category check misses | Planted-loop eval cases escalate ≥95%, no change on control cases |
| SUPP-P2-04 | Pause the bot after handoff until a person hands back (manual session mode) | Parlant pattern; today the bot keeps replying with reassurance | Messages after handoff go to the human queue, not the agent; resume returns control |
| SUPP-P2-05 | Measure handoff timing, not only whether it happened | "Time to Transfer" (AAAI 2021): too-early wastes staff, too-late frustrates | Eval reports turns-to-handoff vs the labelled turn, with tolerance, n shown |
| SUPP-P2-06 | Audit the escalation categories against real owner-handled chats | Categories are a judgement call; real interventions show what's missing | Every owner-handled conversation in the private sample maps to a category, or a new one is added |
| SUPP-P2-07 | Price negotiation / discount requests (36 of ~1,100 real Airbnb questions) | Frequent real ask; Palmgrove has fixed rates today | Policy in KB + routed as policy_exception; eval cases added |
| LEAD-P2-01 | Real WhatsApp sends via the Meta test number | Blocked on 2026-10-20: Meta business verification pending and the payment step rejects Indian cards. The adapter (`integrations/messaging/real.py`) is built and tested against fake HTTP; the Dispatcher's WhatsApp rules (24h window, template, allowlist, opt-out, idempotency) are tested on the mock | `make integrations-check` reports `whatsapp: ok` and the hello_world template arrives on an allowlisted phone; one demo lead gets a real WhatsApp reply |
| VOICE-P2-01 | Resume after a false interruption (cough, "mm-hm", background noise) | LiveKit pattern: speech under 0.5 s or with no transcript within 2 s is treated as false and the agent resumes. Not in Wave 1: the eval has no noise, and a 0.5 s minimum would break the 300 ms barge-in target | A noisy-caller audio set shows the agent resuming after backchannels without missing real barge-ins (≤300 ms) |

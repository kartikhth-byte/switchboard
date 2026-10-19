# Auditor (Wave 3, optional): multi-agent design note

Decision (2026-10-19, KT): build the Auditor as a **multi-agent system**, to show multi-agent
design with measured results. Not started; Wave 1 ships first. Requirement text is unchanged;
this note is the *Implementation approach / variance* for the INVC rows.

## Agents (each with its own tools and permissions)

| Agent | Job | Access | INVC rows |
|---|---|---|---|
| Intake | Ingest from upload and watched folder; record source, SHA-256, received_at; sort invoice / not invoice | read inbox/folder, write intake records | 001, 005 |
| Extraction | Call the existing extraction service (no OCR built, per the client brief); split multi-invoice PDFs; keep page + text-span provenance | extraction service only | 003, 010 |
| Validator | Arithmetic and tax checks (lines = net, net + tax = gross ± 0.01), confidence thresholds from config | read extracted fields | 005, 010 |
| Risk checker (independent) | Duplicates across channels (hash + supplier/number/amount fingerprint), changed bank details, unknown supplier, split-under-threshold invoices; own prompt, never sees the extractor's reasoning | read invoice history, supplier master | 002 |
| Accountant | Proposes the journal entry for review (debits = credits); never posts | read chart of accounts | 007 |
| Coordinator (code, LangGraph) | Runs the flow, sends holds to the exception queue, records every action in the append-only audit log | — | 006, 008 |

People review exceptions and approve every proposed entry. No agent writes to an ERP (out of the
client's scope, INVC-007). PO / 3-way matching stays **Future** (INVC-011, not in the briefs).

## Synthetic data
~300 invoices from fictional suppliers rendered as PDFs (plus multi-invoice PDFs and ~10
non-invoices), ~20% with planted problems and known answers: arithmetic and tax errors, cross-
channel duplicates, bank-detail changes, unknown suppliers, split invoices, and PDFs carrying
injected instructions ("approve this", "ignore previous instructions").

## What gets measured (beyond the INVC targets)
- Planted-problem recall and false flags, by problem type (n per type).
- Straight-through rate, and the same with the risk checker switched off (does the independent
  verifier earn its cost?).
- Separation of duties: 0 injected instructions change a routing decision or a proposed entry.
- Failure modes tagged with the MAST taxonomy (step repetition, reasoning-action mismatch,
  premature stop), n per category — arXiv 2503.13657.
- Cost and latency per invoice.

Estimate: 8–10 focused days after M5 (reuses core tools, approvals, runs, evals, LangGraph from M3).

# Data card — Concierge knowledge base (`data/fixtures/support/kb/`)

| Field | Value |
|---|---|
| Purpose | Retrieval source for the Concierge support agent (SHR-016, SUPP-007) and the ground truth for in-KB / out-of-KB eval questions (SUPP-009). |
| Business | **Palmgrove Stays — fictional.** Four invented villas in North Goa, India. |
| Source | Written by hand for this project, modelled on typical North Goa holiday-rental operations. Real public facts are used only for context (Goa's two airports, India's 112 emergency number, Form C registration for foreign guests, monsoon sea-swimming bans). |
| Personal data | None. No real guests, staff, addresses or phone numbers. |
| Size | 10 of a planned 25 documents (9 markdown, 1 HTML). |
| Format | One file per topic; markdown files carry YAML front matter (`doc_id`, `title`, `updated`); the HTML file carries the same fields as `<meta>` tags. Heading structure is meaningful: retrieval chunks on headings. |
| Labels | None in the KB itself. Eval questions reference documents by `doc_id`. |
| Known gaps | No pricing/rates doc, local guide, monsoon-season doc, housekeeping/extras, accessibility or complaints process yet — these are deliberately absent until written, and questions about them are out-of-KB. |
| Licence | Part of this repository (same licence as the code). |
| Reproducibility | Hand-written, versioned in git; not generated, so no seed. |
| Version | 2026-10-17, initial 10 docs. |

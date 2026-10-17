# Data card — Concierge knowledge base (`data/fixtures/support/kb/`)

| Field | Value |
|---|---|
| Purpose | Retrieval source for the Concierge support agent (SHR-016, SUPP-007) and the ground truth for in-KB / out-of-KB eval questions (SUPP-009). |
| Business | **Palmgrove Stays — fictional.** Four invented villas in North Goa, India. |
| Source | Written by hand for this project, modelled on typical North Goa holiday-rental operations. Real public facts are used only for context (Goa's two airports, India's 112 emergency number, Form C registration for foreign guests, monsoon sea-swimming bans). |
| Personal data | None. No real guests, staff, addresses or phone numbers. |
| Size | 25 documents (24 markdown, 1 HTML), about 8,500 words. |
| Format | One file per topic; markdown files carry YAML front matter (`doc_id`, `title`, `updated`); the HTML file carries the same fields as `<meta>` tags. Heading structure is meaningful: retrieval chunks on headings. |
| Labels | None in the KB itself. Eval questions reference documents by `doc_id`. |
| Deliberately not covered | Specific restaurant names and opening hours, visa rules, medical advice, weather forecasts, other companies' properties, and anything about individual bookings (looked up via a tool, not the KB). Questions on these are out-of-KB and should be declined or escalated. |
| Licence | Part of this repository (same licence as the code). |
| Reproducibility | Hand-written, versioned in git; not generated, so no seed. |
| Version | 2026-10-17, 25 docs. |

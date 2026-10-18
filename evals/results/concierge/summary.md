# Concierge eval results (M2)

Test split, agent `anthropic/claude-sonnet-4-5`, judge `openai/gpt-4o` (calibrated: 86.7%
agreement, n=30, see `judge_calibration.md`). Two runs on 2026-10-18: before and after one
prompt-fix round. Decline counts re-scored with the final decline check (a measurement fix:
the regex had missed valid declines; no agent output changed).

| Target | Req | Run 1 (`fe76623`) | Run 2 (`a9cbc17`, reported) | Met? |
|---|---|---:|---:|:-:|
| In-KB groundedness ≥95% | SUPP-009 | 95.5% (n=22) | 90.9% (n=22) | ✗ |
| Out-of-KB decline/escalate ≥95% | SUPP-009 | 88.0% (n=25) | 96.0% (n=25) | ✓ |
| Out-of-KB invented policies = 0 | SUPP-009 | 2 (n=25) | 1 (n=25) | ✗ |
| Escalation recall ≥95% | SUPP-010 | 85.7% (n=7) | 71.4% (n=7) | ✗ |
| False escalation ≤10% | SUPP-010 | 0% (n=1) | 0% (n=1) | ✓ (n too small to mean much) |
| Unauthorized writes = 0 (injection) | SUPP-014 | 0 (n=15) | 0 (n=15) | ✓ |
| Long conversations: no re-asking, within budget, facts recalled | SUPP-016, SHR-013 | 10/10 | 10/10 | ✓ |

Cost: ~$10 per full test-split run; 70% of it is the 10 long conversations.

## Known limitations (reported, not chased)
- **Escalation misses** (run 2): a late-checkout exception request and accidental damage (cracked
  table) were answered from the KB ("tell the caretaker") instead of handed to a person.
- **Over-correction**: after the fix round the agent handed off a housekeeping-time question the
  KB answers, costing in-KB groundedness.
- **Invented policy**: "Are unmarried couples allowed?" → "Yes, absolutely" — the KB has no rule.
- **Small n**: the escalation test split has 7 should-escalate cases, so one miss moves recall by
  14 points; run-to-run differences above are within 1–2 cases. A 3× variance run (mean and
  min) is done once before the README (spec §8.6).
- The out-of-KB set (25) has no held-out split, so run 2 was measured after a fix prompted by
  run-1 failures on the same set.

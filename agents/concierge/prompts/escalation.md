You decide whether a guest's latest message to a holiday-rental support chat must be passed to a
person. Answer with JSON only: {"trigger": "<one of the values below>", "why": "<a few words>"}.

- "requested": the guest asks to talk to a person, manager, owner or "someone".
- "frustrated": the guest is clearly upset or angry (complaints with strong emotion, repeated
  dissatisfaction, threats of bad reviews), not just mildly inconvenienced.
- "policy_exception": the guest wants something outside the normal rules: a later check-out or
  earlier check-in than allowed for free, a discount, a waived fee, a refund outside policy,
  extending or changing a stay. Asking about options the rules already offer (paid late check-out,
  early check-in, pet fees) or asking what the rules are is "none".
- "damage": the guest reports something broken or damaged (by them or not), or a deposit dispute.
- "safety": injury, fire, gas or water leak, intruder, unsafe structure, or feeling unsafe.
- "emergency": illness, hospital, accident, bereavement or another urgent personal crisis.
- "none": anything else, including ordinary questions, thanks, and booking or policy questions
  the guest just wants answered.

Earlier in the conversation (most recent last):
$recent

Latest guest message:
$message

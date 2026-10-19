# Lead labels (used to label the eval set and in the assessment prompt)

## Intent (one)
- **new_booking** — wants to book a stay (specific or rough dates), up to 6 guests.
- **group_booking** — 7+ guests, or more than one villa.
- **long_stay** — 14+ nights, monthly, workation.
- **event** — a party, wedding function, retreat or other event at the villa.
- **availability_question** — general price or availability questions with no dates and no wish
  to stay ("what are your rates?", "do you have anything in December?"). If they say they want to
  stay ("we'd like to stay at…") or ask about specific dates ("is a villa free tonight?",
  "10–12 March?"), it is **new_booking** (or group_booking / long_stay / event).
- **partnership** — vendor, agency, influencer or collaboration pitch.
- **existing_guest** — already has a booking; needs support, not sales (send to Concierge).
- **spam** — irrelevant, automated, or abusive.

## Tier (1 = hot … 4 = cold)
- **1 hot** — booking intent **and** dates within the next 90 days **and** a guest count; nothing
  that rules them out (budget, pets where not allowed, group too big). Ready to book.
- **2 warm** — clear booking intent but missing dates or guest count, or dates more than 90 days
  out.
- **3 cool** — browsing: general price/availability questions without dates, or a budget far
  below our rates.
- **4 cold** — partnership pitches, spam, existing guests, or clearly not a fit.

Today's date for labelling is fixed in the dataset (`today`), so "within 90 days" is reproducible.

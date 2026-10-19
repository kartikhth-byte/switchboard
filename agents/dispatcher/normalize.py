"""Contact normalization for leads (LEAD-010): phones to E.164, emails lowercased and trimmed,
invalid values flagged rather than guessed. Enrichment is deferred (LEAD-010)."""

from __future__ import annotations

import re

import phonenumbers
from pydantic import BaseModel, Field

DEFAULT_REGION = "IN"  # Palmgrove Stays is in Goa; numbers without a country code are Indian
EMAIL = re.compile(r"^[a-z0-9._%+\-]+@[a-z0-9\-]+(\.[a-z0-9\-]+)*\.[a-z]{2,}$")


class Contact(BaseModel):
    email: str | None = None
    phone: str | None = None  # E.164, e.g. +919876543210
    issues: list[str] = Field(default_factory=list)  # e.g. ["invalid_email", "invalid_phone"]


def normalize_email(raw: str | None) -> tuple[str | None, str | None]:
    """-> (email, issue). Strips whitespace, a mailto: prefix, <> brackets and a trailing dot."""
    if raw is None or not raw.strip():
        return None, None
    s = raw.strip().strip("<>").strip()
    if s.lower().startswith("mailto:"):
        s = s[7:]
    s = s.strip().rstrip(".").lower()
    if " " in s or not EMAIL.match(s) or ".." in s:
        return None, "invalid_email"
    return s, None


def normalize_phone(raw: str | None, region: str = DEFAULT_REGION) -> tuple[str | None, str | None]:
    """-> (E.164, issue). A leading 00 is treated as an international prefix."""
    if raw is None or not raw.strip():
        return None, None
    s = raw.strip()
    if s.startswith("00"):
        s = "+" + s[2:]
    try:
        num = phonenumbers.parse(s, region)
    except phonenumbers.NumberParseException:
        return None, "invalid_phone"
    if not phonenumbers.is_valid_number(num):
        return None, "invalid_phone"
    return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164), None


def normalize(email: str | None, phone: str | None, region: str = DEFAULT_REGION) -> Contact:
    e, e_issue = normalize_email(email)
    p, p_issue = normalize_phone(phone, region)
    return Contact(email=e, phone=p, issues=[i for i in (e_issue, p_issue) if i])


def dedupe_key(c: Contact) -> str | None:
    """Same person across webhook replays and channels (LEAD-005): email first, else phone."""
    return f"email:{c.email}" if c.email else (f"phone:{c.phone}" if c.phone else None)

"""PII redaction for the logging and tracing sinks (SHR-019).

Applied where data leaves the process for observability — the JSON log formatter and
Langfuse's `mask` hook — not inside agent logic, which needs the real values (e.g. to
send a confirmation to a phone number).

Covers: emails, card numbers (Luhn-checked), phone numbers (9–15 digits), and phone
numbers spoken digit-by-digit in voice transcripts ("nine eight four five ...").
"""

from __future__ import annotations

import re
from typing import Any

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# 13–19 digits, optionally grouped by spaces or dashes.
# Boundaries exclude letters and dashes, so digit groups inside UUIDs/trace ids survive.
CARD = re.compile(r"(?<![\w-])(?:\d[ -]?){12,18}\d(?![\w-])")
# Optional +country code, then digit groups separated by space/dot/dash/parens.
PHONE = re.compile(r"(?<![\w+-])\+?\(?\d[\d\s().-]{7,}\d(?![\w-])")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")

_WORD_DIGITS = {
    "zero": "0", "oh": "0", "o": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
}  # fmt: skip
_DIGIT_WORD_RE = r"(?:zero|oh|o|one|two|three|four|five|six|seven|eight|nine|double|triple|\d+)"
# A run of spoken digit tokens separated by spaces, commas or hyphens.
SPOKEN = re.compile(rf"\b{_DIGIT_WORD_RE}(?:[\s,-]+{_DIGIT_WORD_RE}){{4,}}\b", re.IGNORECASE)

MIN_PHONE_DIGITS = 9  # dates (8 digits) and short refs stay readable
MIN_SPOKEN_DIGITS = 7


def _luhn_ok(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _card(m: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", m.group())
    return "[CARD]" if 13 <= len(digits) <= 19 and _luhn_ok(digits) else m.group()


def _phone(m: re.Match[str]) -> str:
    text = m.group()
    if ISO_DATE.match(text.lstrip("+(")):
        return text
    n = sum(ch.isdigit() for ch in text)
    return "[PHONE]" if MIN_PHONE_DIGITS <= n <= 15 else text


def _spoken_digit_count(text: str) -> int:
    count, mult = 0, 1
    for tok in re.split(r"[\s,-]+", text.lower()):
        if tok == "double":
            mult = 2
        elif tok == "triple":
            mult = 3
        elif tok.isdigit():
            count += len(tok) * mult
            mult = 1
        elif tok in _WORD_DIGITS:
            count += mult
            mult = 1
    return count


def _spoken(m: re.Match[str]) -> str:
    return "[PHONE]" if _spoken_digit_count(m.group()) >= MIN_SPOKEN_DIGITS else m.group()


def redact(text: str) -> str:
    text = EMAIL.sub("[EMAIL]", text)
    text = CARD.sub(_card, text)  # before PHONE: a card is also a long digit run
    text = PHONE.sub(_phone, text)
    return SPOKEN.sub(_spoken, text)


def redact_any(data: Any) -> Any:
    """Recursively redact strings in dicts/lists/tuples (Langfuse `mask` hook)."""
    if isinstance(data, str):
        return redact(data)
    if isinstance(data, dict):
        return {k: redact_any(v) for k, v in data.items()}
    if isinstance(data, list | tuple):
        return type(data)(redact_any(v) for v in data)
    return data


def langfuse_mask(*, data: Any, **_: Any) -> Any:
    return redact_any(data)

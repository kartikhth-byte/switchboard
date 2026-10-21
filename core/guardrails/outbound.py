"""Who real outbound messages may reach (LEAD-017; shared by Dispatcher and Operator)."""

from __future__ import annotations


def allowed(recipient: str, allowlist: set[str]) -> bool:
    """Exact address/number, a whole email domain ("@example.org") or a number prefix
    ending in "*" ("+9198765*", for a block of test numbers)."""
    r = recipient.lower().replace(" ", "")
    if r in allowlist or ("@" in r and "@" + r.split("@", 1)[1] in allowlist):
        return True
    return any(a.endswith("*") and r.startswith(a[:-1]) for a in allowlist)

"""Check model-provider keys without printing them. Usage: make keys-check

Makes two tiny live calls (one embedding, one 1-token completion; well under $0.001).
"""

import os
import sys

import litellm

from core.config.settings import export_provider_keys


def shape(value: str) -> str:
    """Describe a key without revealing it."""
    if not value:
        return "not set"
    prefix = "sk-proj-" if value.startswith("sk-proj-") else value[:7]
    issues = []
    if value != value.strip() or any(c.isspace() for c in value):
        issues.append("contains whitespace")
    if value.startswith(("'", '"')) or value.endswith(("'", '"')):
        issues.append("wrapped in quotes")
    return f"{prefix}… ({len(value)} chars)" + (f" — {', '.join(issues)}" if issues else "")


def main() -> int:
    litellm.suppress_debug_info = True
    sources = export_provider_keys()
    ok = True
    for key, source in sources.items():
        print(f"{key}: from {source}, {shape(os.environ.get(key, ''))}")
    checks = [
        ("OpenAI embeddings", lambda: litellm.embedding(
            model="openai/text-embedding-3-small", input=["ping"])),
        ("Anthropic completion", lambda: litellm.completion(
            model="anthropic/claude-haiku-4-5", max_tokens=1,
            messages=[{"role": "user", "content": "ping"}])),
    ]  # fmt: skip
    for name, call in checks:
        try:
            call()
            print(f"  ✓ {name}: key accepted")
        except Exception as exc:  # noqa: BLE001 — report and continue
            ok = False
            print(f"  ✗ {name}: {type(exc).__name__}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

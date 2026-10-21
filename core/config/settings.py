"""Process-wide settings. Secrets come only from the environment or .env (SHR-005).

Per-agent YAML config (SHR-017) lives in core/config/agent.py.
"""

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)

# Read by LiteLLM straight from os.environ, so they must be exported, not just parsed.
PROVIDER_KEYS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "dev"  # Langfuse environment tag: dev | ci | demo
    database_url: str = "postgresql://switchboard:switchboard@localhost:5432/switchboard"

    # Langfuse Cloud by default; self-hosted via the `langfuse` compose profile.
    langfuse_host: str = "https://cloud.langfuse.com"
    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None

    # Safety defaults (LEAD-017, ADR §Non-negotiable safeguards).
    demo_integrations: Literal["mock", "real"] = "mock"
    outbound_mode: Literal["off", "allowlist", "approve_first"] = "off"
    # Recipients (E.164 phones, emails) real outbound may reach when OUTBOUND_MODE=allowlist
    # (LEAD-017). Comma-separated; empty = nobody.
    outbound_allowlist: str = ""
    daily_usd_cap: float = 5.0

    # Real integrations (DEMO_INTEGRATIONS=real only; mocks otherwise). Secrets never logged.
    slack_bot_token: SecretStr | None = None
    slack_channels: str = ""  # "#leads=C0123,#ops-alerts=C0456,#guest-support=C0789"
    whatsapp_phone_number_id: str = ""
    whatsapp_token: SecretStr | None = None
    whatsapp_api_version: str = "v21.0"
    whatsapp_template_language: str = "en"
    ghl_token: SecretStr | None = None  # GoHighLevel Private Integration token
    ghl_location_id: str = ""
    google_service_account_file: str = ""  # JSON key path (mounted read-only into the worker)
    google_calendar_id: str = ""  # the shared test calendar's id (…@group.calendar.google.com)
    smtp_host: str = "localhost"  # Mailpit (compose service `mailpit` inside the network)
    smtp_port: int = 1025
    email_from: str = "Palmgrove Stays <hello@palmgrove.test>"
    # Embeddings for KB retrieval (SHR-016). "hash" = offline lexical embedder for tests/CI.
    embed_model: str = "openai/text-embedding-3-small"
    worker_concurrency: int = 8  # jobs run in parallel per worker batch (LEAD-018)
    rate_limit_per_min: int = 30  # per client IP, public endpoints (SHR-018)


def missing_real_settings(settings: "Settings", needs: tuple[str, ...]) -> list[str]:
    """Env var names still unset for the real integrations in `needs` (slack, whatsapp, ghl)."""
    req = {
        "slack": ["slack_bot_token"],
        "whatsapp": ["whatsapp_phone_number_id", "whatsapp_token"],
        "ghl": ["ghl_token", "ghl_location_id"],
    }
    return [k.upper() for n in needs for k in req[n] if not getattr(settings, k)]


def allowlist(settings: "Settings") -> set[str]:
    return {x.strip().lower() for x in settings.outbound_allowlist.split(",") if x.strip()}


def export_provider_keys(env_file: Path = Path(".env")) -> dict[str, str]:
    """Copy model-provider keys from .env into os.environ so LiteLLM sees them.

    The project's .env wins over a key already exported in the shell: a stale key from
    another project in ~/.zshrc is the usual cause of "Incorrect API key" here.
    Returns {key: source} with source in {".env", "shell", "missing"} (never the values).
    """
    file_vals = dotenv_values(env_file) if env_file.is_file() else {}
    sources: dict[str, str] = {}
    for key in PROVIDER_KEYS:
        from_file = (file_vals.get(key) or "").strip()
        in_shell = os.environ.get(key, "")
        if from_file:
            if in_shell and in_shell != from_file:
                log.warning("%s in your shell differs from .env; using the .env value", key)
            os.environ[key] = from_file
            sources[key] = ".env"
        else:
            sources[key] = "shell" if in_shell else "missing"
    return sources


@lru_cache
def get_settings() -> Settings:
    export_provider_keys()
    return Settings()

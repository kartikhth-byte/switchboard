"""Process-wide settings. Secrets come only from the environment (SHR-005).

Per-agent YAML config (SHR-017) lands in M1; this module only covers env.
"""

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    daily_usd_cap: float = 5.0
    # Embeddings for KB retrieval (SHR-016). "hash" = offline lexical embedder for tests/CI.
    embed_model: str = "openai/text-embedding-3-small"
    rate_limit_per_min: int = 30  # per client IP, public endpoints (SHR-018)


@lru_cache
def get_settings() -> Settings:
    return Settings()

"""Per-agent config (SHR-017): prompts, rules, rubrics and the demo business profile live
in versioned files, so changing behaviour needs no code change.

    agents/<name>/
      config/agent.yaml      model, limits, memory, escalation rules
      config/business.md     demo business profile (fictional)
      prompts/*.md           prompt templates, `$var` placeholders
      rubrics/*.md           judge rubrics (optional)
"""

from __future__ import annotations

from pathlib import Path
from string import Template
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core.guardrails import DEFAULT_RUN_TOKEN_BUDGET, Budget
from core.models import ModelSpec


class ConfigError(ValueError):
    pass


class Limits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_steps: int = Field(default=8, ge=1, le=50)
    run_token_budget: int = Field(default=DEFAULT_RUN_TOKEN_BUDGET, ge=1000)


class MemoryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    window_turns: int = Field(default=8, ge=1)
    summarize_over_tokens: int = Field(default=6000, ge=500)
    model: str | None = None  # "provider/name" for summaries + fact extraction; None = agent model
    facts: list[str] = Field(default_factory=lambda: ["name", "dates", "issue_type"])

    def model_spec(self, fallback: ModelSpec) -> ModelSpec:
        if not self.model:
            return fallback
        provider, _, name = self.model.partition("/")
        return ModelSpec(provider=provider, name=name, max_tokens=300)


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")  # typos in YAML fail loudly

    agent: str
    model: ModelSpec
    business: str | None = None  # path, relative to the agent folder
    kb_path: str | None = None  # path, relative to the repo root
    limits: Limits = Field(default_factory=Limits)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    escalation: dict[str, Any] = Field(default_factory=dict)
    rules: dict[str, Any] = Field(default_factory=dict)  # agent-specific knobs (e.g. tier rules)

    # Loaded from files, not YAML.
    root: Path = Field(default=Path("."), exclude=True)
    business_profile: str = ""
    prompts: dict[str, str] = Field(default_factory=dict)
    rubrics: dict[str, str] = Field(default_factory=dict)

    def prompt(self, name: str, **values: Any) -> str:
        """Render prompts/<name>.md. Missing `$placeholders` raise, rather than ship blank."""
        if name not in self.prompts:
            raise ConfigError(f"{self.agent}: no prompt '{name}' in {self.root / 'prompts'}")
        try:
            return Template(self.prompts[name]).substitute(business=self.business_profile, **values)
        except KeyError as exc:
            raise ConfigError(f"{self.agent}: prompt '{name}' needs ${exc.args[0]}") from exc

    def budget(self, daily_usd: float | None) -> Budget:
        return Budget(run_tokens=self.limits.run_token_budget, daily_usd=daily_usd)


def _read_dir(path: Path) -> dict[str, str]:
    if not path.is_dir():
        return {}
    return {p.stem: p.read_text(encoding="utf-8") for p in sorted(path.glob("*.md"))}


def load_agent_config(agent_dir: str | Path) -> AgentConfig:
    root = Path(agent_dir)
    cfg_file = root / "config" / "agent.yaml"
    if not cfg_file.is_file():
        raise ConfigError(f"missing {cfg_file}")
    try:
        raw = yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{cfg_file}: invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{cfg_file}: top level must be a mapping")
    try:
        cfg = AgentConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"{cfg_file}: {exc}") from exc

    cfg.root = root
    if cfg.business:
        biz = root / cfg.business
        if not biz.is_file():
            raise ConfigError(f"{cfg_file}: business profile not found: {biz}")
        cfg.business_profile = biz.read_text(encoding="utf-8")
    cfg.prompts = _read_dir(root / "prompts")
    cfg.rubrics = _read_dir(root / "rubrics")
    return cfg

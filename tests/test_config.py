import shutil
from pathlib import Path

import pytest

from core.config.agent import ConfigError, load_agent_config
from core.models import ModelClient

FIXTURE = Path(__file__).parent / "fixtures" / "agent_example"


def test_loads_yaml_prompts_rubrics_and_business() -> None:
    cfg = load_agent_config(FIXTURE)
    assert cfg.agent == "concierge"
    assert cfg.model.model_id == "anthropic/claude-sonnet-4-5"
    assert cfg.model.fallback == "openai/gpt-4o-mini"
    assert cfg.limits.max_steps == 6 and cfg.budget(5.0).run_tokens == 20000
    assert "Palmgrove Stays" in cfg.business_profile
    assert set(cfg.prompts) == {"system"} and set(cfg.rubrics) == {"groundedness"}
    rendered = cfg.prompt("system", guest_name="Asha")
    assert "Palmgrove Stays" in rendered and "Asha" in rendered


def test_changing_files_changes_behaviour_without_code(tmp_path: Path) -> None:
    """SHR-017 + SHR-001: switch model and edit a prompt by editing files only."""
    agent = tmp_path / "concierge"
    shutil.copytree(FIXTURE, agent)
    yaml_path = agent / "config" / "agent.yaml"
    yaml_path.write_text(
        yaml_path.read_text().replace(
            "{provider: anthropic, name: claude-sonnet-4-5, fallback: openai/gpt-4o-mini}",
            "{provider: openai, name: gpt-4o-mini}",
        )
    )
    (agent / "prompts" / "system.md").write_text("Be brief. $business")
    cfg = load_agent_config(agent)
    resp = ModelClient(cfg.model).complete([{"role": "user", "content": "hi"}], mock_response="ok")
    assert resp.model == "openai/gpt-4o-mini"
    assert cfg.prompt("system").startswith("Be brief.")


def test_missing_placeholder_fails_loudly() -> None:
    with pytest.raises(ConfigError, match="guest_name"):
        load_agent_config(FIXTURE).prompt("system")


@pytest.mark.parametrize(
    ("edit", "match"),
    [
        (lambda s: s + "\nlimitz: {max_steps: 3}\n", "limitz"),  # typo'd key
        (lambda s: s.replace("max_steps: 6", "max_steps: 0"), "max_steps"),
        (lambda s: s.replace("business: config/business.md", "business: nope.md"), "not found"),
        (lambda s: "agent: [unclosed", "invalid YAML"),
    ],
)
def test_bad_config_rejected(tmp_path: Path, edit: object, match: str) -> None:
    agent = tmp_path / "a"
    shutil.copytree(FIXTURE, agent)
    p = agent / "config" / "agent.yaml"
    p.write_text(edit(p.read_text()))  # type: ignore[operator]
    with pytest.raises(ConfigError, match=match):
        load_agent_config(agent)

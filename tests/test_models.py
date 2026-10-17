from types import SimpleNamespace
from typing import Any

import litellm
import pytest

from core.models import ModelClient, ModelError, ModelSpec
from core.models import client as client_mod

MSGS = [{"role": "user", "content": "What time is check-out?"}]


def test_model_switch_is_config_only() -> None:
    """SHR-001: same code, different `model:` config, different provider."""
    for cfg, expected in [
        ({"provider": "anthropic", "name": "claude-sonnet-4-5"}, "anthropic/claude-sonnet-4-5"),
        ({"provider": "openai", "name": "gpt-4o-mini"}, "openai/gpt-4o-mini"),
    ]:
        r = ModelClient(ModelSpec.model_validate(cfg)).complete(MSGS, mock_response="11am")
        assert r.model == expected
        assert r.content == "11am"


def test_usage_and_cost_recorded() -> None:
    spec = ModelSpec(provider="anthropic", name="claude-sonnet-4-5")
    r = ModelClient(spec).complete(MSGS, mock_response="11am")
    assert r.tokens_in > 0 and r.tokens_out > 0
    assert r.cost_usd > 0  # known model → priced by LiteLLM


def test_unknown_pricing_does_not_break(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_price(**_: Any) -> float:
        raise ValueError("model not mapped")

    monkeypatch.setattr(litellm, "completion_cost", no_price)
    r = ModelClient(ModelSpec(provider="anthropic", name="x")).complete(MSGS, mock_response="ok")
    assert r.cost_usd == 0.0 and r.content == "ok"


def _fake_raw(tool_args: str) -> Any:
    tc = SimpleNamespace(id="c1", function=SimpleNamespace(name="search_kb", arguments=tool_args))
    msg = SimpleNamespace(content=None, tool_calls=[tc])
    usage = SimpleNamespace(prompt_tokens=5, completion_tokens=3)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=usage)


def test_fallback_used_when_primary_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake_completion(model: str, **_: Any) -> Any:
        seen.append(model)
        if model.startswith("anthropic/"):
            raise litellm.exceptions.APIConnectionError(
                message="down", llm_provider="anthropic", model=model
            )
        return _fake_raw('{"query": "pool hours"}')

    monkeypatch.setattr(client_mod.litellm, "completion", fake_completion)
    monkeypatch.setattr(client_mod.litellm, "completion_cost", lambda **_: 0.001)
    spec = ModelSpec(provider="anthropic", name="claude-sonnet-4-5", fallback="openai/gpt-4o-mini")
    r = ModelClient(spec).complete(MSGS)
    assert seen == ["anthropic/claude-sonnet-4-5", "openai/gpt-4o-mini"]
    assert r.used_fallback and r.model == "openai/gpt-4o-mini"
    assert r.tool_calls[0].arguments == {"query": "pool hours"}


def test_all_models_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(model: str, **_: Any) -> Any:
        raise RuntimeError("nope")

    monkeypatch.setattr(client_mod.litellm, "completion", boom)
    with pytest.raises(ModelError):
        ModelClient(ModelSpec(provider="anthropic", name="a", fallback="openai/b")).complete(MSGS)


def test_invalid_tool_json_marked_not_guessed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_mod.litellm, "completion", lambda **_: _fake_raw("{not json"))
    monkeypatch.setattr(client_mod.litellm, "completion_cost", lambda **_: 0.0)
    r = ModelClient(ModelSpec(provider="anthropic", name="a")).complete(MSGS)
    assert r.tool_calls[0].arguments is None
    assert r.tool_calls[0].raw_arguments == "{not json"

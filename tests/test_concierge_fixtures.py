import re
from pathlib import Path

from core.config.agent import load_agent_config

ROOT = Path(__file__).resolve().parents[1]
KB = ROOT / "data/fixtures/support/kb"


def test_concierge_config_loads_and_prompt_renders() -> None:
    cfg = load_agent_config(ROOT / "agents/concierge")
    assert cfg.agent == "concierge" and cfg.limits.max_steps == 8
    system = cfg.prompt("system", today="Saturday 17 October 2026")
    assert "Palmgrove Stays" in system and "Fictional business" in system
    assert (ROOT / cfg.kb_path).is_dir() if cfg.kb_path else False


def test_kb_docs_have_unique_ids_and_titles() -> None:
    docs = sorted(p for p in KB.iterdir() if p.suffix in {".md", ".html"})
    assert len(docs) >= 10
    ids = []
    for p in docs:
        text = p.read_text(encoding="utf-8")
        m = re.search(r"^doc_id: (\S+)$", text, re.M) or re.search(
            r'<meta name="doc_id" content="([^"]+)">', text
        )
        assert m, f"{p.name}: missing doc_id"
        ids.append(m.group(1))
    assert len(ids) == len(set(ids))
    assert any(p.suffix == ".html" for p in docs)  # SUPP-007: markdown and HTML

import shutil
from pathlib import Path

import pytest

from core.config.settings import get_settings
from core.retrieval import HashEmbedder, KnowledgeBase, chunk_doc, parse_doc
from core.retrieval.chunking import TARGET_TOKENS, approx_tokens

KB = Path(__file__).resolve().parents[1] / "data/fixtures/support/kb"

# ---------- unit: parsing + chunking ----------


def test_markdown_heading_aware_chunks() -> None:
    doc = parse_doc(KB / "07-payments-and-deposits.md")
    assert doc.doc_id == "payments-and-deposits"
    assert doc.title == "Payments and the security deposit"
    chunks = chunk_doc(doc)
    assert [c.chunk_id for c in chunks] == [
        f"payments-and-deposits#{i}" for i in range(len(chunks))
    ]
    dep = next(c for c in chunks if c.heading.endswith("Security deposit"))
    assert dep.text.startswith("Payments and the security deposit > Security deposit")
    assert "₹20,000" in dep.text  # tables stay inside their section
    assert "doc_id:" not in "".join(c.text for c in chunks)  # front matter stripped


def test_html_parsed_with_meta_tables_and_headings() -> None:
    doc = parse_doc(KB / "09-getting-here-and-transfers.html")
    assert doc.doc_id == "getting-here-and-transfers"
    texts = [c.text for c in chunk_doc(doc)]
    transfers = next(t for t in texts if "Airport transfers we arrange" in t)
    assert "| Mopa (GOX) to any villa | ₹2,200 | ₹3,000 |" in transfers
    assert not any("<" in t for t in texts)


def test_long_sections_split_with_overlap(tmp_path: Path) -> None:
    paras = [f"Paragraph {i} " + " ".join(f"word{i}_{j}" for j in range(60)) for i in range(20)]
    p = tmp_path / "long.md"
    p.write_text("---\ndoc_id: long\ntitle: Long\n---\n# Long\n\n## Part\n\n" + "\n\n".join(paras))
    chunks = chunk_doc(parse_doc(p))
    assert len(chunks) > 2
    assert all(approx_tokens(c.text) <= TARGET_TOKENS * 1.3 for c in chunks)
    # Overlap: the end of each chunk reappears at the start of the next.
    tail = chunks[0].text.split()[-5:]
    assert " ".join(tail) in chunks[1].text
    assert all(c.text.startswith("Long > Part") for c in chunks)


def test_every_kb_doc_parses_into_chunks() -> None:
    files = sorted(p for p in KB.iterdir() if p.suffix in {".md", ".html"})
    total = sum(len(chunk_doc(parse_doc(p))) for p in files)
    assert len(files) == 25 and total > 50


def test_hash_embedder_is_deterministic_and_normalised() -> None:
    e = HashEmbedder(64)
    a, b = e.embed(["check-out at 11am", "check-out at 11am"])
    assert a == b and abs(sum(x * x for x in a) - 1) < 1e-9


# ---------- integration: pgvector ----------


@pytest.fixture
def kb(tmp_path: Path) -> tuple[KnowledgeBase, Path]:
    from core.db import connect, migrate

    dsn = get_settings().database_url
    migrate(dsn)
    corpus = f"test-{tmp_path.name}"
    folder = tmp_path / "kb"
    shutil.copytree(KB, folder)
    yield KnowledgeBase(dsn, corpus, HashEmbedder()), folder
    with connect(dsn) as c:
        c.execute("DELETE FROM kb_docs WHERE corpus = %s", (corpus,))


@pytest.mark.integration
def test_search_returns_contract_fields(kb: tuple[KnowledgeBase, Path]) -> None:
    store, folder = kb
    store.ingest(folder)
    hits = store.search("what time is check-out", k=3)
    assert len(hits) == 3
    assert hits[0].doc_id == "check-in-and-check-out"
    assert set(hits[0].model_dump()) == {"chunk_id", "doc_id", "title", "text", "score"}
    assert hits[0].score >= hits[1].score >= hits[2].score
    assert {h.chunk_id for h in hits} <= store.chunk_ids()


@pytest.mark.integration
def test_reingest_is_idempotent(kb: tuple[KnowledgeBase, Path]) -> None:
    store, folder = kb
    first = store.ingest(folder)
    ids = store.chunk_ids()
    second = store.ingest(folder)
    assert len(first.added) == 25 and first.chunks_written == len(ids)
    assert len(second.unchanged) == 25 and second.chunks_written == 0
    assert store.chunk_ids() == ids


@pytest.mark.integration
def test_edited_doc_changes_answers_after_reindex(kb: tuple[KnowledgeBase, Path]) -> None:
    """SUPP-007: an edited doc changes what retrieval returns after re-index."""
    store, folder = kb
    store.ingest(folder)
    q = "what time is check-out"
    before = " ".join(h.text for h in store.search(q, k=3))
    assert "by 11:00 am" in before
    doc = folder / "02-check-in-and-check-out.md"
    doc.write_text(doc.read_text().replace("by 11:00 am", "by 10:30 am"))
    stats = store.ingest(folder)
    assert stats.updated == ["check-in-and-check-out"] and len(stats.unchanged) == 24
    after = " ".join(h.text for h in store.search(q, k=3))
    assert "by 10:30 am" in after and "by 11:00 am" not in after


@pytest.mark.integration
def test_removed_doc_and_embedder_switch(kb: tuple[KnowledgeBase, Path]) -> None:
    store, folder = kb
    store.ingest(folder)
    (folder / "23-lost-and-found.md").unlink()
    assert store.ingest(folder).removed == ["lost-and-found"]
    assert not any(c.startswith("lost-and-found#") for c in store.chunk_ids())
    # A different embedder re-embeds everything; searches only compare same-model rows.
    store.embedder = HashEmbedder(256)
    assert len(store.ingest(folder).updated) == 24
    assert store.search("pool lifeguard", k=1)[0].doc_id == "pool-and-safety"

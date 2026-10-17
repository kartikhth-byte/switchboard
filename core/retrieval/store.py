"""KB ingestion and search over pgvector (SHR-016, SUPP-007).

ingest(): idempotent by content hash. Unchanged docs are skipped; edited docs have their
chunks replaced; docs removed from the folder are deleted. Changing the embedder re-embeds.
search(): cosine similarity, returns {chunk_id, doc_id, title, text, score}.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from core.db import connect
from core.retrieval.chunking import chunk_doc, parse_doc
from core.retrieval.embeddings import Embedder

KB_SUFFIXES = {".md", ".markdown", ".html", ".htm"}


class Hit(BaseModel):
    chunk_id: str
    doc_id: str
    title: str
    text: str
    score: float


@dataclass
class IngestStats:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    chunks_written: int = 0


def _vec(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


class KnowledgeBase:
    def __init__(self, dsn: str, corpus: str, embedder: Embedder) -> None:
        self.dsn = dsn
        self.corpus = corpus
        self.embedder = embedder

    def ingest(self, folder: Path) -> IngestStats:
        stats = IngestStats()
        docs = [parse_doc(p) for p in sorted(folder.iterdir()) if p.suffix.lower() in KB_SUFFIXES]
        ids = [d.doc_id for d in docs]
        if len(ids) != len(set(ids)):
            raise ValueError(f"duplicate doc_id in {folder}")
        with connect(self.dsn) as c:
            existing: dict[str, tuple[str, str]] = {
                str(r["doc_id"]): (str(r["content_hash"]), str(r["embed_model"]))
                for r in c.execute(
                    "SELECT doc_id, content_hash, embed_model FROM kb_docs WHERE corpus = %s",
                    (self.corpus,),
                ).fetchall()
            }
        for doc in docs:
            prev = existing.get(doc.doc_id)
            if prev == (doc.content_hash, self.embedder.name):
                stats.unchanged.append(doc.doc_id)
                continue
            chunks = chunk_doc(doc)
            vectors = self.embedder.embed([ch.text for ch in chunks]) if chunks else []
            with connect(self.dsn) as c, c.transaction():
                c.execute(
                    "INSERT INTO kb_docs"
                    " (corpus, doc_id, title, source_path, content_hash, embed_model)"
                    " VALUES (%s, %s, %s, %s, %s, %s)"
                    " ON CONFLICT (corpus, doc_id) DO UPDATE SET title = EXCLUDED.title,"
                    " source_path = EXCLUDED.source_path, content_hash = EXCLUDED.content_hash,"
                    " embed_model = EXCLUDED.embed_model, updated_at = now()",
                    (
                        self.corpus,
                        doc.doc_id,
                        doc.title,
                        str(doc.path),
                        doc.content_hash,
                        self.embedder.name,
                    ),
                )
                c.execute(
                    "DELETE FROM kb_chunks WHERE corpus = %s AND doc_id = %s",
                    (self.corpus, doc.doc_id),
                )
                for ch, vec in zip(chunks, vectors, strict=True):
                    c.execute(
                        "INSERT INTO kb_chunks (corpus, chunk_id, doc_id, ord, heading, text,"
                        " embed_model, embedding) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::vector)",
                        (
                            self.corpus,
                            ch.chunk_id,
                            ch.doc_id,
                            ch.ord,
                            ch.heading,
                            ch.text,
                            self.embedder.name,
                            _vec(vec),
                        ),
                    )
            stats.chunks_written += len(chunks)
            (stats.updated if prev else stats.added).append(doc.doc_id)
        gone = sorted(set(existing) - set(ids))
        if gone:
            with connect(self.dsn) as c:
                c.execute(
                    "DELETE FROM kb_docs WHERE corpus = %s AND doc_id = ANY(%s)",
                    (self.corpus, gone),
                )
            stats.removed = gone
        return stats

    def search(self, query: str, k: int = 5) -> list[Hit]:
        [qv] = self.embedder.embed([query])
        with connect(self.dsn) as c:
            rows: list[dict[str, Any]] = c.execute(
                "SELECT ch.chunk_id, ch.doc_id, d.title, ch.text,"
                " 1 - (ch.embedding <=> %(q)s::vector) AS score"
                " FROM kb_chunks ch JOIN kb_docs d USING (corpus, doc_id)"
                " WHERE ch.corpus = %(corpus)s AND ch.embed_model = %(model)s"
                " ORDER BY ch.embedding <=> %(q)s::vector LIMIT %(k)s",
                {"q": _vec(qv), "corpus": self.corpus, "model": self.embedder.name, "k": k},
            ).fetchall()
        return [Hit.model_validate(r) for r in rows]

    def chunk_ids(self) -> set[str]:
        with connect(self.dsn) as c:
            rows = c.execute(
                "SELECT chunk_id FROM kb_chunks WHERE corpus = %s", (self.corpus,)
            ).fetchall()
        return {str(r["chunk_id"]) for r in rows}

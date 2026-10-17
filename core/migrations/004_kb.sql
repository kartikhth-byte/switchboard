-- SHR-016: knowledge-base documents and chunks for retrieval (pgvector).
-- `embedding` has no fixed dimension so the embedder can be swapped; each row records its
-- model and searches only compare rows from the same model. ~100 chunks: exact scan, no index.
CREATE TABLE IF NOT EXISTS kb_docs (
    corpus        text NOT NULL,          -- e.g. 'support'
    doc_id        text NOT NULL,
    title         text NOT NULL,
    source_path   text NOT NULL,
    content_hash  text NOT NULL,          -- re-ingest skips unchanged docs (idempotent)
    embed_model   text NOT NULL,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (corpus, doc_id)
);

CREATE TABLE IF NOT EXISTS kb_chunks (
    corpus       text NOT NULL,
    chunk_id     text NOT NULL,
    doc_id       text NOT NULL,
    ord          integer NOT NULL,
    heading      text NOT NULL,
    text         text NOT NULL,
    embed_model  text NOT NULL,
    embedding    vector NOT NULL,
    PRIMARY KEY (corpus, chunk_id),
    FOREIGN KEY (corpus, doc_id) REFERENCES kb_docs (corpus, doc_id) ON DELETE CASCADE
);

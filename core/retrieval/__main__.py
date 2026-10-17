"""python -m core.retrieval ingest|search ...   (make ingest / make search)"""

import argparse
import sys
from pathlib import Path

from core.config.settings import get_settings
from core.db import migrate
from core.retrieval import KnowledgeBase, make_embedder


def main() -> int:
    s = get_settings()
    p = argparse.ArgumentParser(prog="python -m core.retrieval")
    p.add_argument("command", choices=["ingest", "search"])
    p.add_argument("--corpus", default="support")
    p.add_argument("--path", default="data/fixtures/support/kb")
    p.add_argument(
        "--embedder", default=s.embed_model, help="e.g. openai/text-embedding-3-small, hash"
    )
    p.add_argument("-q", "--query", default="")
    p.add_argument("-k", type=int, default=5)
    a = p.parse_args()
    migrate(s.database_url)
    kb = KnowledgeBase(s.database_url, a.corpus, make_embedder(a.embedder))
    if a.command == "ingest":
        st = kb.ingest(Path(a.path))
        print(
            f"{a.corpus} [{a.embedder}]: added {len(st.added)}, updated {len(st.updated)},"
            f" unchanged {len(st.unchanged)}, removed {len(st.removed)},"
            f" chunks written {st.chunks_written}"
        )
    else:
        for h in kb.search(a.query, a.k):
            print(f"{h.score:.3f}  {h.chunk_id:40s}  {h.text.splitlines()[0][:70]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

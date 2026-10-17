"""Heading-aware chunking for markdown and HTML KB docs (SHR-016, SUPP-007).

Each chunk keeps its heading path ("Payments and the security deposit > Security deposit")
so it reads standalone. Sections over ~500 tokens are split on paragraphs with ~50 tokens
of overlap. PDF ingestion is optional (cut list) and not implemented.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

CHUNKER_VERSION = "1"  # bump to force re-ingest when chunking changes
TARGET_TOKENS = 500
OVERLAP_TOKENS = 50


@dataclass(frozen=True)
class Doc:
    doc_id: str
    title: str
    path: Path
    sections: list[tuple[str, str]]  # (heading path, body)
    content_hash: str


@dataclass(frozen=True)
class Chunk:
    chunk_id: str  # "<doc_id>#<n>"
    doc_id: str
    title: str
    heading: str
    text: str
    ord: int


def approx_tokens(text: str) -> int:
    """Cheap estimate (~1.3 tokens per word); exactness doesn't matter for chunk sizing."""
    return int(len(text.split()) * 1.3) + 1


# ---- markdown ----

_FRONT = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_HEADING = re.compile(r"^(#{1,3})\s+(.*)$")


def _front_matter(text: str) -> tuple[dict[str, str], str]:
    m = _FRONT.match(text)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip('"')
    return meta, text[m.end() :]


def _sections_from_lines(lines: list[tuple[int | None, str]]) -> list[tuple[str, str]]:
    """lines: (heading level or None, text). Builds (heading path, body) sections."""
    path: list[str] = []
    sections: list[tuple[str, str]] = []
    body: list[str] = []

    def flush() -> None:
        text = "\n".join(body).strip()
        if text:
            sections.append((" > ".join(path), text))
        body.clear()

    for level, text in lines:
        if level is None:
            body.append(text)
            continue
        flush()
        del path[level - 1 :]
        path.append(text.strip())
    flush()
    return sections


def parse_markdown(path: Path) -> Doc:
    raw = path.read_text(encoding="utf-8")
    meta, body = _front_matter(raw)
    lines: list[tuple[int | None, str]] = []
    for line in body.splitlines():
        m = _HEADING.match(line)
        lines.append((len(m.group(1)), m.group(2)) if m else (None, line))
    title = meta.get("title") or next((t for lvl, t in lines if lvl == 1), path.stem)
    return Doc(
        doc_id=meta.get("doc_id", path.stem),
        title=title,
        path=path,
        sections=_sections_from_lines(lines),
        content_hash=_hash(raw),
    )


# ---- HTML ----


class _HTMLText(HTMLParser):
    BLOCK = {"p", "li", "tr", "br", "div", "article", "section", "table", "ul", "ol"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title = ""
        self.lines: list[tuple[int | None, str]] = []
        self._buf: list[str] = []
        self._heading: int | None = None
        self._in_title = False
        self._skip = 0
        self._cells: list[str] | None = None

    def _flush(self) -> None:
        text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        self._buf.clear()
        if text:
            self.lines.append((self._heading, text))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "meta" and a.get("name") and a.get("content"):
            self.meta[str(a["name"])] = str(a["content"])
        elif tag == "title":
            self._in_title = True
        elif tag in ("h1", "h2", "h3"):
            self._flush()
            self._heading = int(tag[1])
        elif tag == "tr":
            self._flush()
            self._cells = []
        elif tag in ("td", "th") and self._cells is not None:
            self._buf.clear()
        elif tag == "li":
            self._flush()
            self._buf.append("- ")
        elif tag in self.BLOCK:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in ("h1", "h2", "h3"):
            self._flush()
            self._heading = None
        elif tag in ("td", "th") and self._cells is not None:
            self._cells.append(re.sub(r"\s+", " ", "".join(self._buf)).strip())
            self._buf.clear()
        elif tag == "tr" and self._cells is not None:
            self.lines.append((None, "| " + " | ".join(self._cells) + " |"))
            self._cells = None
        elif tag in self.BLOCK:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._in_title:
            self.title += data
            return
        self._buf.append(data)


def parse_html(path: Path) -> Doc:
    raw = path.read_text(encoding="utf-8")
    p = _HTMLText()
    p.feed(raw)
    p._flush()
    title = p.title.strip() or next((t for lvl, t in p.lines if lvl == 1), path.stem)
    return Doc(
        doc_id=p.meta.get("doc_id", path.stem),
        title=title,
        path=path,
        sections=_sections_from_lines(p.lines),
        content_hash=_hash(raw),
    )


def parse_doc(path: Path) -> Doc:
    if path.suffix.lower() in (".md", ".markdown"):
        return parse_markdown(path)
    if path.suffix.lower() in (".html", ".htm"):
        return parse_html(path)
    raise ValueError(f"unsupported KB file type: {path.name}")


# ---- chunking ----


def chunk_doc(doc: Doc) -> list[Chunk]:
    chunks: list[Chunk] = []
    for heading, body in doc.sections:
        for piece in _split(body):
            n = len(chunks)
            chunks.append(
                Chunk(
                    chunk_id=f"{doc.doc_id}#{n}",
                    doc_id=doc.doc_id,
                    title=doc.title,
                    heading=heading,
                    text=f"{heading}\n\n{piece}" if heading else piece,
                    ord=n,
                )
            )
    return chunks


def _split(body: str) -> list[str]:
    if approx_tokens(body) <= TARGET_TOKENS:
        return [body]
    paras = [p for p in re.split(r"\n\s*\n", body) if p.strip()]
    out: list[str] = []
    cur: list[str] = []
    for para in paras:
        if cur and approx_tokens("\n\n".join([*cur, para])) > TARGET_TOKENS:
            out.append("\n\n".join(cur))
            # Carry the tail of the previous chunk forward as overlap.
            tail = " ".join(" ".join(cur).split()[-int(OVERLAP_TOKENS / 1.3) :])
            cur = [tail]
        cur.append(para)
    if cur:
        out.append("\n\n".join(cur))
    return out


def _hash(raw: str) -> str:
    return hashlib.sha256(f"{CHUNKER_VERSION}\n{raw}".encode()).hexdigest()

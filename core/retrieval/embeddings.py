"""Embedders. `openai/text-embedding-3-small` for real runs; `hash` for tests and CI (no key)."""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol

import litellm

_WORD = re.compile(r"[a-z0-9₹]+")


class Embedder(Protocol):
    name: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class LiteLLMEmbedder:
    def __init__(self, model: str = "openai/text-embedding-3-small", batch: int = 64) -> None:
        self.name = model
        self.batch = batch

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch):
            resp = litellm.embedding(model=self.name, input=texts[i : i + self.batch])
            out.extend([float(x) for x in d["embedding"]] for d in resp.data)
        return out


class HashEmbedder:
    """Deterministic bag-of-words hashed into a fixed vector. Lexical, not semantic: good
    enough to test the pipeline offline; never used for reported eval numbers."""

    def __init__(self, dim: int = 512) -> None:
        self.name = f"hash-{dim}"
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        words = _WORD.findall(text.lower())
        for w in words + [f"{a}_{b}" for a, b in zip(words, words[1:], strict=False)]:
            h = int(hashlib.md5(w.encode(), usedforsecurity=False).hexdigest(), 16)
            v[h % self.dim] += 1.0 if (h >> 64) & 1 else -1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]


def make_embedder(name: str) -> Embedder:
    if name.startswith("hash"):
        dim = int(name.split("-")[1]) if "-" in name else 512
        return HashEmbedder(dim)
    return LiteLLMEmbedder(name)

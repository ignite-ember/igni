"""Pluggable text-embedder for the code-intelligence vector index.

The neo4j vector index on :Chunk stores 384-dim cosine embeddings.
``Embedder`` is the seam between :class:`CodeIndex` and whatever
produces them:

- **Tests / offline runs**: pass a deterministic stub
  (``ZeroEmbedder`` / ``HashEmbedder``) so the suite doesn't need a
  live model.
- **Production**: pass a model-backed embedder (Anthropic /
  OpenAI-compatible) so the vector index is actually meaningful
  for semantic search.

The pluggable seam keeps ``CodeIndex`` decoupled from the model
registry and lets a caller swap providers without touching the
indexer. The default ``HashEmbedder`` is deterministic and
unit-test friendly (two texts with identical chars get the same
vector), which is what the unit tests want.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Protocol

# Default vector dimension; must match the vector index DDL in
# ``neo4j_schema.COMMIT_SCHEMA_STATEMENTS`` (384). Centralised here
# so adding a new embedder is one number to change.
EMBEDDING_DIM: int = 384


class Embedder(Protocol):
    """Produce a 384-dim vector for each input text.

    Implementations MUST return one float list per input string,
    in the same order. Vectors SHOULD be unit-length so the
    configured cosine similarity is meaningful.
    """

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class ZeroEmbedder:
    """Deterministic 384-dim zero vectors.

    The simplest possible embedder — every text maps to ``[0.0, ...]``.
    Use this for tests that don't care about semantic ranking and
    just want the wire shape exercised.
    """

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[0.0] * EMBEDDING_DIM for _ in texts]


class HashEmbedder:
    """Deterministic 384-dim hash-derived unit vectors.

    Each text's SHA-256 digest is split into 384 × 32-bit chunks
    and L2-normalized. Two texts with identical content get the
    same vector (important for the "re-upsert" semantics in
    :class:`Neo4jClient.upsert_item`); different texts almost
    always differ in most coordinates. Useful for offline tests
    that want *some* separation between distinct chunks without
    loading a model.
    """

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            # 384 floats × 4 bytes = 1536 bytes. SHA-256 is only 32
            # bytes, so we tile the digest to fill 1536 bytes (384 × 4).
            # ``hash()`` modulo biases each 32-bit word toward
            # small values; that's fine for the unit-test purpose
            # (just needs *some* non-zero separation).
            buf = (digest * (1536 // len(digest) + 1))[:1536]
            vec = [
                int.from_bytes(buf[i * 4 : i * 4 + 4], "big", signed=True) / 2**31
                for i in range(EMBEDDING_DIM)
            ]
            # L2-normalize so cosine similarity = dot product.
            norm = sum(x * x for x in vec) ** 0.5 or 1.0
            out.append([x / norm for x in vec])
        return out


class LiveEmbedder:
    """Adapter that turns :mod:`core.embeddings`'s model-backed
    embedder into the :class:`Embedder` protocol.

    The default model is the project-wide all-MiniLM-L6-v2 (384-dim)
    so this matches the vector index's dimension exactly. Sync —
    the project's model loader (:func:`embeddings.embed_sync`) runs
    on a worker thread internally, so a sync call here doesn't
    block the event loop in practice.
    """

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        # Local import keeps the embedder module free of a hard
        # dependency on the project's model loader (which pulls
        # in sentence-transformers + a real model on first use).
        from ember_code.core.embeddings import embed_sync

        return embed_sync(texts)


__all__ = [
    "EMBEDDING_DIM",
    "Embedder",
    "HashEmbedder",
    "LiveEmbedder",
    "ZeroEmbedder",
]

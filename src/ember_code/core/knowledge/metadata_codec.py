"""Chroma metadata codec + content hashing for the knowledge index.

ChromaDB requires flat, scalar metadata values, so nested ``metadata``
dicts have to round-trip through a ``meta.<key>`` prefix. Both sides
of that round-trip plus the 16-hex content hash that becomes each
entry's stable id live here, on a single class, so the invariant
``unflatten(flatten(x)) == x`` (over the ``meta.*`` subset) can't drift
across call sites.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from pydantic import BaseModel


class ChunkMetadata(BaseModel):
    """Typed metadata for one knowledge chunk row.

    Mirrors the parent-side :meth:`KnowledgeMetadataCodec.flatten`
    contract for chunks — replaces the ad-hoc ``dict[str, object]``
    literal that used to live inside
    :meth:`KnowledgeIndex.add_document`. ``to_chroma_dict`` produces
    the ``dict[str, str]`` Chroma actually stores.
    """

    parent_doc_id: str
    chunk_index: int
    name: str
    source: str

    def to_chroma_dict(self) -> dict[str, str]:
        """Serialize to the flat ``dict[str, str]`` Chroma metadata accepts."""
        return {
            "parent_doc_id": self.parent_doc_id,
            "chunk_index": str(self.chunk_index),
            "name": self.name,
            "source": self.source,
        }


class KnowledgeMetadataCodec:
    """Encodes/decodes knowledge entry metadata for ChromaDB.

    Chroma stores metadata as a flat ``dict[str, str]``, so extras get
    an ``meta.`` prefix on the way in and are stripped back on the way
    out. Keeps the invariant + hash policy in one place — replaces
    three free functions in the old ``index.py`` plus a duplicate
    hash helper that lived in ``sync.py``.

    The codec is stateless today, so a shared instance and a
    freshly-constructed one are equivalent — callers that later want
    to attach state (custom hash length, alternate prefix) should be
    aware that sync and non-sync callers will diverge unless the same
    instance is threaded through both.
    """

    META_PREFIX: str = "meta."
    HASH_HEX_LEN: int = 16

    def flatten(
        self,
        *,
        entry_id: str,
        name: str,
        source: str,
        extras: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Flatten entry metadata into Chroma's scalar-only shape.

        Reverse of :meth:`unflatten`.
        """
        out: dict[str, str] = {
            "entry_id": entry_id,
            "name": name,
            "source": source,
        }
        for k, v in (extras or {}).items():
            if k and v is not None:
                out[f"{self.META_PREFIX}{k}"] = str(v)
        return out

    def flatten_chunk(
        self,
        *,
        entry_id: str,
        chunk_index: int,
        name: str,
        source: str,
    ) -> ChunkMetadata:
        """Build a typed :class:`ChunkMetadata` for one chunk row.

        Mirrors :meth:`flatten` for the parent side. Kept on the codec
        so per-chunk encode policy stays centralized instead of
        leaking into :meth:`KnowledgeIndex.add_document`.

        Chunk-id generation (``"{entry_id}::{i}"``) intentionally stays
        on :meth:`ChunksCollection.replace_for_parent` — this method
        returns metadata only, so the two responsibilities don't get
        tangled.
        """
        return ChunkMetadata(
            parent_doc_id=entry_id,
            chunk_index=chunk_index,
            name=name,
            source=source,
        )

    def unflatten(self, flat: dict[str, str] | None) -> dict[str, str]:
        """Pull ``meta.<k>`` keys back into a nested dict."""
        out: dict[str, str] = {}
        for k, v in (flat or {}).items():
            if k.startswith(self.META_PREFIX):
                out[k[len(self.META_PREFIX) :]] = str(v)
        return out

    def content_hash(self, content: str) -> str:
        """Stable 16-hex sha256 prefix — used as an entry's canonical id."""
        return hashlib.sha256(content.encode()).hexdigest()[: self.HASH_HEX_LEN]

    def now_iso(self) -> str:
        """UTC ISO-8601 timestamp used for entry provenance.

        Sits on the codec because the codec already owns entry-id
        policy (``content_hash``) — keeping the timestamp policy on
        the same object means one injected dependency covers both
        sides of :meth:`KnowledgeEntry.from_content`.
        """
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

"""Metadata codec + id policy for the artifact store.

Three policies live here so they can't drift across call sites:

* **Artifact ids.** ``content_hash`` of the artifact's *name* — not
  its content. The old knowledge store hashed content, which made the
  id change on every edit; an artifact has to keep one id across
  twenty revisions or nothing can point at it.
* **Version ids.** ``artifact_id::n``, derivable from the two things a
  caller already has, so no lookup is needed to address a version.
* **Timestamps.** One UTC ISO-8601 spelling, injectable for tests.

The ``meta.*`` flatten/unflatten pair survives from the Chroma era
because the Neo4j store still round-trips caller-supplied extras
through a single JSON property, and the prefix keeps them from
colliding with the store's own keys.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from pydantic import BaseModel


class ChunkMetadata(BaseModel):
    """Typed metadata for one artifact chunk row."""

    parent_id: str
    chunk_index: int
    name: str
    source: str

    def to_dict(self) -> dict[str, str]:
        return {
            "parent_id": self.parent_id,
            "chunk_index": str(self.chunk_index),
            "name": self.name,
            "source": self.source,
        }


class ArtifactMetadataCodec:
    """Encodes artifact metadata and owns id + timestamp policy.

    The codec is stateless today, so a shared instance and a freshly
    constructed one are equivalent — callers that later attach state
    (custom hash length, alternate prefix) should know that sync and
    non-sync callers will diverge unless the same instance is threaded
    through both.
    """

    META_PREFIX: str = "meta."
    HASH_HEX_LEN: int = 16

    def flatten(
        self,
        *,
        artifact_id: str,
        name: str,
        source: str,
        extras: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Flatten version metadata into a scalar-only shape.

        Reverse of :meth:`unflatten`.
        """
        out: dict[str, str] = {
            "artifact_id": artifact_id,
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
        version_id: str,
        chunk_index: int,
        name: str,
        source: str,
    ) -> ChunkMetadata:
        """Build a typed :class:`ChunkMetadata` for one chunk row."""
        return ChunkMetadata(
            parent_id=version_id,
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
        """Stable 16-hex sha256 prefix."""
        return hashlib.sha256(content.encode()).hexdigest()[: self.HASH_HEX_LEN]

    def artifact_id(self, name: str) -> str:
        """Derive a stable artifact id from its name.

        Hashing the *name* rather than the content is what lets an
        artifact keep one identity across revisions — content-hashed
        ids were the reason the knowledge store could not version
        anything. Names are normalised (case-folded, whitespace
        collapsed) so "Auth Design" and "auth  design" don't become
        two artifacts that shadow each other in search.
        """
        normalised = re.sub(r"\s+", " ", (name or "").strip().casefold())
        if not normalised:
            raise ValueError("artifact_id requires a non-empty name")
        return self.content_hash(f"artifact:{normalised}")

    def version_id(self, artifact_id: str, n: int) -> str:
        """Address a version without a lookup."""
        return f"{artifact_id}::{n}"

    def now_iso(self) -> str:
        """UTC ISO-8601 timestamp used for provenance.

        Sits on the codec because the codec already owns id policy —
        one injected dependency covers both sides of
        :meth:`ArtifactEntry.from_content`.
        """
        return datetime.now(timezone.utc).isoformat()

"""Composed handle-pair for the knowledge index.

:class:`KnowledgeStore` bundles the two chroma collection wrappers the
index actually holds — parents (``knowledge_documents``) and chunks
(``knowledge_chunks``) — into one Pydantic value. This kills the pair
of ``DocumentsCollection | None`` / ``ChunksCollection | None``
optional attributes that used to live on :class:`KnowledgeIndex` and
forced every method to reassert the not-``None`` invariant.

The store also owns the per-entry delete atom (:meth:`delete_entry`),
which returns a :class:`DeleteOutcome` Result so the delete-by-query
loop can accumulate errors without a try/except at every call site —
closing the Pattern-3 error-accumulator hole in the audit.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, ConfigDict

from ember_code.core.knowledge.collections import (
    ChunksCollection,
    DocumentsCollection,
)
from ember_code.core.knowledge.models import DeleteOutcome

logger = logging.getLogger(__name__)


class KnowledgeStore(BaseModel):
    """Two-collection handle pair the knowledge index operates against.

    Held as a single ``KnowledgeStore | None`` on
    :class:`KnowledgeIndex` (composed value, not wire schema) —
    ``arbitrary_types_allowed`` is intentional here because the
    fields are collection *wrappers*, not serializable data. This is
    the first arbitrary-types model in the package; keep it out of
    :mod:`ember_code.core.knowledge.__init__` so it doesn't get read
    as a public wire type by mistake.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    docs: DocumentsCollection
    chunks: ChunksCollection

    async def delete_entry(self, entry_id: str) -> DeleteOutcome:
        """Delete one parent doc + its chunk fan-out. Never raises.

        Returns :class:`DeleteOutcome.success` on a clean delete and
        :class:`DeleteOutcome.fail` on any exception — the caller
        composes N outcomes into a :class:`KnowledgeDeleteResult`.
        The broad ``Exception`` catch preserves the swallow-scope of
        the previous ``_delete_doc`` implementation.
        """
        try:
            await self.docs.delete(entry_id)
            await self.chunks.delete_by_parent(entry_id)
        except Exception as exc:
            logger.exception("delete failed for %s", entry_id)
            return DeleteOutcome.fail(entry_id, str(exc))
        return DeleteOutcome.success(entry_id)

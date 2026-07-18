"""On-disk persistence adapter for the code_index manifest.

Stored as JSON at ``~/.ember/projects/<project_id>/code_index/manifest.json``.
Tracks which commits are indexed, when each was last touched (for the
30-day retention rule), and which branches each one is currently on
(for the branch-pin retention rule).

The domain models (:class:`CommitInfo`, :class:`ManifestState`,
:class:`ManifestWire`, :class:`Clock`) live in
:mod:`ember_code.core.code_index.schema.manifest` — this module keeps
only the I/O concerns (path resolution + atomic JSON write) and the
:meth:`ManifestStore.mutate` chokepoint that unifies the old
load-mutate-save copies.

The historical class name :class:`Manifest` is preserved as an alias
so downstream imports (``from ...manifest import Manifest``) continue
to resolve. The Pydantic types are re-exported here as well so
``from ...manifest import ManifestState`` (used under ``TYPE_CHECKING``
in the backend) still works after the split.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from collections.abc import Callable
from pathlib import Path

from ember_code.core.code_index.paths import code_index_dir, manifest_path
from ember_code.core.code_index.schema.manifest import (
    Clock,
    CommitInfo,
    ManifestLoadResult,
    ManifestState,
    ManifestWire,
    SystemClock,
)

logger = logging.getLogger(__name__)


class ManifestStore:
    """Read/write helper for the project's commit manifest.

    Owns path resolution + atomic JSON I/O. Domain mutations are
    delegated to methods on :class:`ManifestState` via the single
    :meth:`mutate` chokepoint — the store never reaches into
    :attr:`ManifestState.commits` directly.
    """

    def __init__(
        self,
        *,
        project: str | Path,
        data_dir: str | Path = "~/.ember",
        clock: Clock | None = None,
    ):
        self.project = project
        self.data_dir = data_dir
        self.path = manifest_path(project, data_dir=data_dir)
        self.code_index_dir = code_index_dir(project, data_dir=data_dir)
        self._clock: Clock = clock or SystemClock()

    def load(self) -> ManifestState:
        """Return the parsed manifest, or a fresh empty state on missing/corrupt files.

        Kept as a plain :class:`ManifestState` return type for
        backward compat with the 6+ existing call sites that use
        ``.load().commits`` / ``.load().head``. New callers that want
        to distinguish missing vs corrupt should use
        :meth:`load_result` instead.
        """
        return self.load_result().state

    def load_result(self) -> ManifestLoadResult:
        """Return a :class:`ManifestLoadResult` with a ``reason`` code.

        ``reason`` is ``"missing"`` for absent files, ``"corrupt"``
        for unparseable JSON, and ``None`` on success. Lets the sync
        manager surface manifest corruption instead of silently
        starting from a fresh state.
        """
        if not self.path.exists():
            return ManifestLoadResult(state=ManifestState(), ok=False, reason="missing")
        try:
            data = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            logger.warning("Could not parse manifest at %s; starting fresh", self.path)
            return ManifestLoadResult(state=ManifestState(), ok=False, reason="corrupt")
        wire = ManifestWire.parse_dict(data, clock=self._clock)
        return ManifestLoadResult(state=wire.to_state(), ok=True, reason=None)

    def save(self, state: ManifestState) -> None:
        """Atomically write ``state`` to disk.

        Per-writer tmp filename so two concurrent saves (one BE, N
        sessions in the same project) don't clobber each other's tmp
        and trip ``os.replace`` with ``FileNotFoundError``.
        ``os.replace`` is atomic, so the last writer wins safely.
        """
        self.code_index_dir.mkdir(parents=True, exist_ok=True)
        payload = ManifestWire.from_state(state).model_dump(mode="json")
        tmp = self.path.with_suffix(self.path.suffix + f".tmp.{os.getpid()}.{uuid.uuid4().hex}")
        tmp.write_text(json.dumps(payload, indent=2))
        os.replace(tmp, self.path)

    def mutate(self, fn: Callable[[ManifestState], None]) -> ManifestState:
        """Single load-mutate-save chokepoint.

        Replaces the five copy-pasted load/mutate/save blocks the
        old ``Manifest`` carried on ``upsert_commit`` / ``touch`` /
        ``set_head`` / ``remove_commit`` / ``update_branch_refs``.
        Each of those becomes a one-liner that delegates to a
        :class:`ManifestState` method through this chokepoint.
        """
        state = self.load()
        fn(state)
        self.save(state)
        return state

    def upsert_commit(
        self,
        sha: str,
        *,
        branch_refs: list[str] | None = None,
    ) -> ManifestState:
        return self.mutate(
            lambda s: s.upsert_commit(sha, clock=self._clock, branch_refs=branch_refs)
        )

    def touch(self, sha: str) -> ManifestState:
        return self.mutate(lambda s: s.touch(sha, clock=self._clock))

    def set_head(self, sha: str) -> ManifestState:
        return self.mutate(lambda s: s.set_head(sha, clock=self._clock))

    def remove_commit(self, sha: str) -> ManifestState:
        return self.mutate(lambda s: s.remove_commit(sha))

    def update_branch_refs(self, refs: dict[str, list[str]]) -> ManifestState:
        return self.mutate(lambda s: s.update_branch_refs(refs))


# Backwards-compat alias — the persistence adapter was called
# ``Manifest`` historically; downstream code still imports it by
# that name. Keeping the alias avoids a big-bang import migration.
Manifest = ManifestStore


__all__ = [
    "Clock",
    "CommitInfo",
    "Manifest",
    "ManifestLoadResult",
    "ManifestState",
    "ManifestStore",
    "ManifestWire",
    "SystemClock",
]

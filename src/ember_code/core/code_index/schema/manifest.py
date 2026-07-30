"""Domain models for the per-commit code_index manifest.

Home of the Pydantic types that used to be anaemic ``@dataclass``
containers in :mod:`ember_code.core.code_index.manifest`. All
mutations that touch these fields live here as methods on the class
that owns them (Rule 6) — the persistence adapter in
:mod:`~ember_code.core.code_index.manifest` becomes a thin I/O
shim over these models.

The public shape (:class:`CommitInfo` fields, :class:`ManifestState`
fields, on-disk JSON layout) is preserved byte-for-byte so existing
``manifest.json`` files keep round-tripping. :class:`ManifestWire`
carries the on-disk shape (``commits`` as a list) while
:class:`ManifestState` carries the in-memory shape
(``commits`` as a ``dict[sha, CommitInfo]``) — the two-way conversion
lives on :class:`ManifestWire` as :meth:`from_state` / :meth:`to_state`.

The :class:`Clock` collaborator abstracts the ``now_iso`` timestamp
source so mutators are testable without patching ``datetime``.
The default :class:`SystemClock` matches the old free ``_now_iso``
helper's format (``isoformat(timespec="seconds")``).
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field


class Clock:
    """Abstract time source used by manifest mutators.

    Subclass and override :meth:`now_iso` to freeze time in tests
    without monkey-patching :mod:`datetime`.
    """

    def now_iso(self) -> str:
        raise NotImplementedError


class SystemClock(Clock):
    """Default clock — UTC ``isoformat(timespec="seconds")``.

    Matches the old module-level ``_now_iso`` helper's format so
    existing on-disk timestamps keep parsing.
    """

    def now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")


class CommitInfo(BaseModel):
    """Per-commit lineage entry.

    Field order is load-bearing: :meth:`model_dump` produces the
    exact JSON shape (``sha``, ``last_used_at``, ``branch_refs``)
    that the previous ``@dataclass`` + ``asdict`` combo emitted, so
    manifests written by older builds keep round-tripping.
    """

    sha: str
    last_used_at: str  # ISO-8601 UTC
    branch_refs: list[str] = Field(default_factory=list)

    def mark_used(self, clock: Clock) -> None:
        """Refresh ``last_used_at`` to ``clock.now_iso()``."""
        self.last_used_at = clock.now_iso()

    def set_refs(self, refs: list[str]) -> None:
        """Replace ``branch_refs`` with a copy of ``refs``."""
        self.branch_refs = list(refs)


class ManifestState(BaseModel):
    """In-memory manifest state — the domain model.

    Mutators live here (not on the persistence adapter) because
    they only touch fields owned by this class. The persistence
    adapter (:class:`~ember_code.core.code_index.manifest.ManifestStore`)
    reads/writes but doesn't reach into commit dicts.
    """

    head: str | None = None
    commits: dict[str, CommitInfo] = Field(default_factory=dict)

    def upsert_commit(
        self,
        sha: str,
        *,
        clock: Clock,
        branch_refs: list[str] | None = None,
    ) -> None:
        """Insert or refresh a commit entry.

        When ``branch_refs`` is ``None`` the existing refs are
        preserved (matches the old :meth:`Manifest.upsert_commit`
        behaviour). ``last_used_at`` is always advanced.
        """
        existing = self.commits.get(sha)
        refs = (
            list(branch_refs)
            if branch_refs is not None
            else (existing.branch_refs if existing else [])
        )
        self.commits[sha] = CommitInfo(
            sha=sha,
            last_used_at=clock.now_iso(),
            branch_refs=refs,
        )

    def touch(self, sha: str, *, clock: Clock) -> None:
        """Advance ``last_used_at`` for ``sha``. No-op if untracked."""
        info = self.commits.get(sha)
        if info is None:
            return
        info.mark_used(clock)

    def set_head(self, sha: str, *, clock: Clock) -> None:
        """Mark ``sha`` as head; auto-upserts a missing commit."""
        info = self.commits.get(sha)
        if info is None:
            self.commits[sha] = CommitInfo(sha=sha, last_used_at=clock.now_iso())
        else:
            info.mark_used(clock)
        self.head = sha

    def remove_commit(self, sha: str) -> None:
        """Drop a commit entry and clear ``head`` if it pointed there."""
        self.commits.pop(sha, None)
        if self.head == sha:
            self.head = None

    def update_branch_refs(self, refs: dict[str, list[str]]) -> None:
        """Replace ``branch_refs`` on every tracked commit with
        ``refs[sha]`` (empty list if the sha is missing from ``refs``)."""
        for sha, info in self.commits.items():
            info.set_refs(refs.get(sha, []))


class ManifestWire(BaseModel):
    """On-disk JSON shape for the manifest.

    ``commits`` is a ``list`` here (matches the on-disk layout the
    old ``Manifest.save`` produced with ``asdict``); the in-memory
    :class:`ManifestState` keeps a ``dict`` for O(1) lookup.
    :meth:`from_state` / :meth:`to_state` bridge the two shapes.
    """

    head: str | None = None
    commits: list[CommitInfo] = Field(default_factory=list)

    @classmethod
    def from_state(cls, state: ManifestState) -> ManifestWire:
        """Snapshot a :class:`ManifestState` into wire form.

        Iteration order over ``state.commits`` is preserved so the
        on-disk order matches the insertion order — the old
        ``asdict`` loop had the same behaviour.
        """
        return cls(
            head=state.head,
            commits=[state.commits[sha] for sha in state.commits],
        )

    def to_state(self) -> ManifestState:
        """Rebuild an in-memory :class:`ManifestState`.

        Preserves the on-disk commit ordering as the dict's
        insertion order.
        """
        return ManifestState(
            head=self.head,
            commits={info.sha: info for info in self.commits},
        )

    @classmethod
    def parse_dict(cls, data: dict, *, clock: Clock) -> ManifestWire:
        """Lenient parse for older / partial manifest payloads.

        Older manifests may have missed ``last_used_at`` or carried
        malformed entries. Fill missing timestamps with
        ``clock.now_iso()`` and skip entries without an ``sha`` — the
        old :meth:`Manifest.load` had the same forgiveness. Passing the
        cleaned dict to :meth:`model_validate` keeps Pydantic's strict
        type checks in force for well-formed inputs.
        """
        raw_commits = data.get("commits", []) or []
        cleaned: list[dict] = []
        for entry in raw_commits:
            if not isinstance(entry, dict) or "sha" not in entry:
                continue
            cleaned.append(
                {
                    "sha": entry["sha"],
                    "last_used_at": entry.get("last_used_at") or clock.now_iso(),
                    "branch_refs": list(entry.get("branch_refs") or []),
                }
            )
        return cls.model_validate({"head": data.get("head"), "commits": cleaned})


class ManifestLoadResult(BaseModel):
    """Result-style wrapper distinguishing missing vs corrupt manifests.

    ``reason`` is ``"missing"`` when the file didn't exist,
    ``"corrupt"`` when JSON parsing failed, and ``None`` on success.
    Callers that only need the state can use
    :meth:`~ember_code.core.code_index.manifest.ManifestStore.load` —
    those that want to surface corruption should use
    :meth:`~ember_code.core.code_index.manifest.ManifestStore.load_result`.
    """

    state: ManifestState
    ok: bool
    reason: str | None = None


__all__ = [
    "Clock",
    "CommitInfo",
    "ManifestLoadResult",
    "ManifestState",
    "ManifestWire",
    "SystemClock",
]

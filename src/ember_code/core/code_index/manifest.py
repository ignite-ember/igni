"""On-disk manifest for the per-commit code_index lineage.

Stored as JSON at ``~/.ember/projects/<project_id>/code_index/manifest.json``.
Tracks which commits are indexed, when each was last touched (for the
30-day retention rule), and which branches each one is currently on
(for the branch-pin retention rule).

Keeping the file simple — just JSON, written atomically via a tmp +
``os.replace`` rename.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ember_code.core.code_index.paths import code_index_dir, manifest_path

logger = logging.getLogger(__name__)


@dataclass
class CommitInfo:
    sha: str
    last_used_at: str  # ISO-8601 UTC
    branch_refs: list[str] = field(default_factory=list)


@dataclass
class ManifestState:
    head: str | None = None
    commits: dict[str, CommitInfo] = field(default_factory=dict)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Manifest:
    """Read/write helper for the project's commit manifest."""

    def __init__(self, *, project: str | Path, data_dir: str | Path = "~/.ember"):
        self.project = project
        self.data_dir = data_dir
        self.path = manifest_path(project, data_dir=data_dir)
        self.code_index_dir = code_index_dir(project, data_dir=data_dir)

    def load(self) -> ManifestState:
        if not self.path.exists():
            return ManifestState()
        try:
            data = json.loads(self.path.read_text())
        except Exception:
            logger.warning("Could not parse manifest at %s; starting fresh", self.path)
            return ManifestState()
        commits_raw = data.get("commits", []) or []
        commits = {}
        for entry in commits_raw:
            if not isinstance(entry, dict) or "sha" not in entry:
                continue
            commits[entry["sha"]] = CommitInfo(
                sha=entry["sha"],
                last_used_at=entry.get("last_used_at") or _now_iso(),
                branch_refs=list(entry.get("branch_refs") or []),
            )
        return ManifestState(head=data.get("head"), commits=commits)

    def save(self, state: ManifestState) -> None:
        self.code_index_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "head": state.head,
            "commits": [asdict(state.commits[sha]) for sha in state.commits],
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        os.replace(tmp, self.path)

    def upsert_commit(
        self,
        sha: str,
        *,
        branch_refs: list[str] | None = None,
    ) -> ManifestState:
        state = self.load()
        existing = state.commits.get(sha)
        info = CommitInfo(
            sha=sha,
            last_used_at=_now_iso(),
            branch_refs=list(branch_refs)
            if branch_refs is not None
            else (existing.branch_refs if existing else []),
        )
        state.commits[sha] = info
        self.save(state)
        return state

    def touch(self, sha: str) -> ManifestState:
        """Update ``last_used_at`` to now. No-op if the commit isn't tracked."""
        state = self.load()
        info = state.commits.get(sha)
        if info is None:
            return state
        info.last_used_at = _now_iso()
        self.save(state)
        return state

    def set_head(self, sha: str) -> ManifestState:
        """Mark ``sha`` as the current head. Auto-upserts the commit if missing."""
        state = self.load()
        if sha not in state.commits:
            state.commits[sha] = CommitInfo(sha=sha, last_used_at=_now_iso())
        else:
            state.commits[sha].last_used_at = _now_iso()
        state.head = sha
        self.save(state)
        return state

    def remove_commit(self, sha: str) -> ManifestState:
        state = self.load()
        state.commits.pop(sha, None)
        if state.head == sha:
            state.head = None
        self.save(state)
        return state

    def update_branch_refs(self, refs: dict[str, list[str]]) -> ManifestState:
        """Replace ``branch_refs`` for every tracked commit with ``refs[sha]``."""
        state = self.load()
        for sha, info in state.commits.items():
            info.branch_refs = list(refs.get(sha, []))
        self.save(state)
        return state

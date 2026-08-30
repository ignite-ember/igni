"""Sync the group's agents into the project, without eating local edits.

The server is where a team's agents are written now, but a person still
has to be able to open one and change a line. Those two facts collide
the moment an admin edits the same agent, so this module is the merge
between them — the same three-way shape
:class:`~ember_code.core.init.checksum_store.ChecksumStore` uses for
bundled files, with one deliberate difference.

``ChecksumStore.sync_file`` resolves a divergence by dropping a ``.new``
sidecar, bumping the stored hash, and moving on. That is fine for a file
nobody was told about. It is not fine here: the person asked for their
edit to be kept, and silently recording the incoming hash means we never
mention it again. So a divergence becomes a **pending conflict** — the
incoming version is parked next to the file, the local file is left
exactly as it is, and nothing is resolved until somebody says which one
they want.

Everything else follows from "the person did not touch it":

* No local copy → take the server's.
* Local copy untouched since the last sync → take the server's. This is
  the common case and it must not ask anything.
* Agent gone from the group, local copy untouched → remove it. A group
  switch has to actually swap the agents; leaving the old team's behind
  would be the worst kind of half-applied.
* Agent gone from the group, local copy edited → keep it, and say so.

Destination is ``<project>/.ember/agents/`` — the same directory the
bundled agents were scaffolded into, and the same checksum file. That
sharing is on purpose: when a group supplies agents the bundle stands
down, so the first sync after joining a group is a clean handover for
anyone who never edited their copy.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ember_code.core.init.checksum_store import ChecksumStore
from ember_code.core.init.json_file import JsonFile
from ember_code.core.init.schemas import InitConfig

logger = logging.getLogger(__name__)

#: Where pending conflicts live, next to ``.checksums.json``.
CONFLICTS_FILE = ".group-agent-conflicts.json"


class AgentConflict(BaseModel):
    """One agent the server changed under a local edit.

    ``incoming_path`` is null for ``removed``: there is no incoming
    content, the group simply no longer ships this agent.
    """

    entry_name: str
    kind: str  # changed | removed
    incoming_hash: str
    incoming_path: Path | None = None

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def question(self) -> str:
        """What to put in front of the person, in one line."""
        if self.kind == "removed":
            return (
                f"{self.entry_name}: your group no longer ships this agent, "
                f"but you have edited your copy. Remove it?"
            )
        return (
            f"{self.entry_name}: your group changed this agent and you have "
            f"edited your copy. Take the group's version?"
        )


class GroupSyncReport(BaseModel):
    """What one sync did, and what it could not decide alone."""

    copied: list[str] = Field(default_factory=list)
    updated: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    unchanged: list[str] = Field(default_factory=list)
    conflicts: list[AgentConflict] = Field(default_factory=list)

    @property
    def changed_anything(self) -> bool:
        """Whether the agent pool needs rebuilding after this."""
        return bool(self.copied or self.updated or self.removed)


class GroupAgentSync:
    """Merge the cached group pack's agents into a project.

    ``source_dir`` is the pristine server copy that
    :class:`~ember_code.core.config.group_policy.GroupPolicyCache`
    materialises; this class never writes to it. ``project_dir`` is the
    checkout whose ``.ember/agents`` the person actually edits.
    """

    def __init__(
        self,
        project_dir: Path,
        source_dir: Path,
        config: InitConfig | None = None,
    ) -> None:
        self._project_dir = project_dir
        self._source_dir = source_dir
        self._config = config or InitConfig()

    # ── Paths ────────────────────────────────────────────────────

    @property
    def dest_dir(self) -> Path:
        return self._project_dir / ".ember" / "agents"

    @property
    def _conflicts_path(self) -> Path:
        return self._project_dir / ".ember" / CONFLICTS_FILE

    @staticmethod
    def _key(entry_name: str) -> str:
        """Checksum key for an agent — shared with the bundled sync, so
        a handover from bundle to group is a clean update rather than a
        phantom conflict."""
        return f"agents/{entry_name}.md"

    # ── Pending conflicts ────────────────────────────────────────

    def pending(self) -> list[AgentConflict]:
        """Conflicts waiting on an answer. Fail-soft: an unreadable file
        means none, because the alternative is refusing to start."""
        raw = JsonFile(path=self._conflicts_path).load()
        out: list[AgentConflict] = []
        for entry_name, payload in (raw or {}).items():
            try:
                out.append(AgentConflict(entry_name=entry_name, **payload))
            except Exception:  # noqa: BLE001 — a bad record is not worth a crash
                logger.debug("Skipping unreadable conflict record for %s", entry_name)
        return out

    def _save_pending(self, conflicts: list[AgentConflict]) -> None:
        JsonFile(path=self._conflicts_path).save(
            {
                c.entry_name: {
                    "kind": c.kind,
                    "incoming_hash": c.incoming_hash,
                    "incoming_path": str(c.incoming_path) if c.incoming_path else None,
                }
                for c in conflicts
            }
        )

    def resolve(self, entry_name: str, *, accept_incoming: bool) -> bool:
        """Answer one conflict. Returns True if anything on disk moved.

        Either answer records the incoming hash, so the same change is
        not raised twice — keeping your version means keeping it until
        the group changes the agent *again*, not being asked every time
        the CLI starts.
        """
        conflicts = self.pending()
        match = next((c for c in conflicts if c.entry_name == entry_name), None)
        if match is None:
            return False

        store = ChecksumStore.load(self._project_dir, self._config)
        dest = self.dest_dir / f"{entry_name}.md"
        touched = False

        if accept_incoming:
            if match.kind == "removed":
                dest.unlink(missing_ok=True)
                store.entries.pop(self._key(entry_name), None)
            elif match.incoming_path and Path(match.incoming_path).exists():
                shutil.copy2(match.incoming_path, dest)
                store.entries[self._key(entry_name)] = match.incoming_hash
            touched = True
        else:
            # Their edit stands. Record the hash we did not apply so the
            # question is not asked again for the same change.
            store.entries[self._key(entry_name)] = match.incoming_hash

        store.save()
        if match.incoming_path:
            Path(match.incoming_path).unlink(missing_ok=True)
        self._save_pending([c for c in conflicts if c.entry_name != entry_name])
        return touched

    # ── The sync ─────────────────────────────────────────────────

    def run(self) -> GroupSyncReport:
        """Merge the source directory into the project. Idempotent."""
        report = GroupSyncReport()
        if not self._source_dir.is_dir():
            return report

        self.dest_dir.mkdir(parents=True, exist_ok=True)
        store = ChecksumStore.load(self._project_dir, self._config)
        # Anything unresolved stays unresolved unless this run supersedes
        # it, so an unanswered question survives a restart.
        conflicts = {c.entry_name: c for c in self.pending()}

        incoming = sorted(self._source_dir.glob("*.md"))
        for src in incoming:
            entry_name = src.stem
            conflict = self._sync_one(src, entry_name, store, report)
            if conflict is not None:
                conflicts[entry_name] = conflict
            else:
                conflicts.pop(entry_name, None)

        self._prune(
            keep={src.stem for src in incoming},
            store=store,
            report=report,
            conflicts=conflicts,
        )

        store.save()
        report.conflicts = list(conflicts.values())
        self._save_pending(report.conflicts)
        return report

    def _sync_one(
        self,
        src: Path,
        entry_name: str,
        store: ChecksumStore,
        report: GroupSyncReport,
    ) -> AgentConflict | None:
        key = self._key(entry_name)
        dest = self.dest_dir / f"{entry_name}.md"
        incoming_hash = ChecksumStore.file_hash(src)
        stored_hash = store.entries.get(key)

        if not dest.exists():
            shutil.copy2(src, dest)
            store.entries[key] = incoming_hash
            report.copied.append(entry_name)
            return None

        local_hash = ChecksumStore.file_hash(dest)

        if stored_hash is None:
            # Never tracked. Identical content is nothing to argue about;
            # different content is somebody's file we did not put there.
            if local_hash == incoming_hash:
                store.entries[key] = incoming_hash
                report.unchanged.append(entry_name)
                return None
            return self._park(src, entry_name, incoming_hash, kind="changed")

        if incoming_hash == stored_hash:
            report.unchanged.append(entry_name)
            return None

        if local_hash == stored_hash:
            # Untouched since the last sync — take theirs without asking.
            shutil.copy2(src, dest)
            store.entries[key] = incoming_hash
            report.updated.append(entry_name)
            return None

        return self._park(src, entry_name, incoming_hash, kind="changed")

    def _park(self, src: Path, entry_name: str, incoming_hash: str, *, kind: str) -> AgentConflict:
        """Put the incoming version beside the local one and ask later."""
        incoming_path = self.dest_dir / f"{entry_name}.md.incoming"
        shutil.copy2(src, incoming_path)
        return AgentConflict(
            entry_name=entry_name,
            kind=kind,
            incoming_hash=incoming_hash,
            incoming_path=incoming_path,
        )

    def _prune(
        self,
        keep: set[str],
        store: ChecksumStore,
        report: GroupSyncReport,
        conflicts: dict[str, AgentConflict],
    ) -> None:
        """Remove agents the group no longer ships.

        Only ones we put there and nobody has touched. A group switch has
        to actually swap the agents — but an edited file is the person's
        work, and deleting it because an admin moved them to another team
        would be indefensible.
        """
        for key, stored_hash in list(store.entries.items()):
            if not key.startswith("agents/") or not key.endswith(".md"):
                continue
            entry_name = key[len("agents/") : -len(".md")]
            if entry_name in keep:
                continue

            dest = self.dest_dir / f"{entry_name}.md"
            if not dest.exists():
                store.entries.pop(key, None)
                continue

            if ChecksumStore.file_hash(dest) == stored_hash:
                dest.unlink()
                store.entries.pop(key, None)
                report.removed.append(entry_name)
                conflicts.pop(entry_name, None)
            else:
                conflicts[entry_name] = AgentConflict(
                    entry_name=entry_name,
                    kind="removed",
                    incoming_hash=stored_hash,
                )

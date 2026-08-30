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

#: kind → (directory under ``.ember``, filename for one entry). These
#: are the kinds somebody might reasonably open and change: prompts,
#: routines, rules. The rest — MCP servers, plugin installs, hook
#: declarations, Python tools — are configuration the server owns, read
#: straight from the policy cache, and are not synced here.
SYNCED_KINDS: dict[str, tuple[str, str]] = {
    "agents": ("agents", "{name}.md"),
    "skills": ("skills", "{name}/SKILL.md"),
    "commands": ("commands", "{name}.md"),
    "rules": ("rules", "{name}.md"),
    "output-styles": ("output-styles", "{name}.md"),
    "workflows": ("workflows", "{name}.mjs"),
}


#: What to call each kind when talking to a person about one.
KIND_NOUN: dict[str, str] = {
    "agents": "agent",
    "skills": "skill",
    "commands": "command",
    "rules": "rule",
    "output-styles": "output style",
    "workflows": "workflow",
}


class EntryConflict(BaseModel):
    """One thing the group changed under somebody's local edit.

    ``change`` is what happened on the server — ``changed`` or
    ``removed``. ``entry_kind`` is what the thing *is*; the two used to
    share the name ``kind``, which read fine until a skill needed one.

    ``incoming_path`` is null for ``removed``: there is no incoming
    content, the group simply no longer ships it.
    """

    entry_kind: str
    entry_name: str
    change: str  # changed | removed
    incoming_hash: str
    incoming_path: Path | None = None

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @property
    def id(self) -> str:
        """Stable identifier — two kinds may hold the same name."""
        return f"{self.entry_kind}/{self.entry_name}"

    def question(self) -> str:
        """What to put in front of the person, in one line."""
        noun = KIND_NOUN.get(self.entry_kind, "entry")
        if self.change == "removed":
            return (
                f'Your group no longer ships the {noun} "{self.entry_name}", but you have '
                f"edited your copy. Remove it, or keep yours?"
            )
        return (
            f'Your group changed the {noun} "{self.entry_name}" and you have edited your '
            f"copy. Take the group's version, or keep yours?"
        )


#: Kept so older imports do not break mid-refactor.
AgentConflict = EntryConflict


class GroupSyncReport(BaseModel):
    """What one sync did, and what it could not decide alone."""

    copied: list[str] = Field(default_factory=list)
    updated: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    unchanged: list[str] = Field(default_factory=list)
    conflicts: list[EntryConflict] = Field(default_factory=list)

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
        kind: str = "agents",
    ) -> None:
        self._project_dir = project_dir
        self._source_dir = source_dir
        self._config = config or InitConfig()
        self._kind = kind
        self._subdir, self._filename = SYNCED_KINDS[kind]

    # ── Paths ────────────────────────────────────────────────────

    @property
    def dest_dir(self) -> Path:
        return self._project_dir / ".ember" / self._subdir

    @property
    def _conflicts_path(self) -> Path:
        return self._project_dir / ".ember" / CONFLICTS_FILE

    def _key(self, entry_name: str) -> str:
        """Checksum key for one entry.

        Agents and skills share theirs with the bundled sync, so a
        handover from what igni ships to what the group ships is a clean
        update rather than a phantom conflict.
        """
        return f"{self._subdir}/{self._filename.format(name=entry_name)}"

    # ── Pending conflicts ────────────────────────────────────────

    def _all_pending(self) -> list[EntryConflict]:
        """Every unanswered question in the project, of any kind."""
        raw = JsonFile(path=self._conflicts_path).load()
        out: list[EntryConflict] = []
        for record_id, payload in (raw or {}).items():
            try:
                out.append(EntryConflict(**payload))
            except Exception:  # noqa: BLE001 — a bad record is not worth a crash
                logger.debug("Skipping unreadable conflict record for %s", record_id)
        return out

    def pending(self) -> list[EntryConflict]:
        """Unanswered questions about this kind."""
        return [c for c in self._all_pending() if c.entry_kind == self._kind]

    def pending_all(self) -> list[EntryConflict]:
        """Unanswered questions of every kind, for the person to answer."""
        return self._all_pending()

    def _save_pending(self, conflicts: list[EntryConflict]) -> None:
        """One file for every kind, keyed ``<kind>/<name>``.

        Shared rather than one per kind because it is one question list
        as far as the person is concerned, and because a reader wants
        all of it at once.
        """
        existing = {c.id: c for c in self._all_pending()}
        mine = {c.id for c in self._all_pending() if c.entry_kind == self._kind}
        for stale in mine:
            existing.pop(stale, None)
        for c in conflicts:
            existing[c.id] = c
        JsonFile(path=self._conflicts_path).save(
            {
                c.id: {
                    "entry_kind": c.entry_kind,
                    "entry_name": c.entry_name,
                    "change": c.change,
                    "incoming_hash": c.incoming_hash,
                    "incoming_path": str(c.incoming_path) if c.incoming_path else None,
                }
                for c in existing.values()
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
        dest = self._dest_for(entry_name)
        touched = False

        if accept_incoming:
            if match.change == "removed":
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

    def _source_for(self, entry_name: str) -> Path:
        """Where the cache put this entry."""
        return self._source_dir / self._filename.format(name=entry_name)

    def _dest_for(self, entry_name: str) -> Path:
        """Where one entry lands. A skill is a directory with SKILL.md
        inside it; everything else is a file."""
        return self.dest_dir / self._filename.format(name=entry_name)

    def _entry_names(self) -> list[str]:
        """What the group ships, read off the cache directory."""
        if not self._source_dir.is_dir():
            return []
        if self._filename.startswith("{name}/"):
            leaf = self._filename.split("/", 1)[1]
            return sorted(p.name for p in self._source_dir.iterdir() if (p / leaf).is_file())
        suffix = self._filename.replace("{name}", "")
        return sorted(
            p.name[: -len(suffix)] for p in self._source_dir.iterdir() if p.name.endswith(suffix)
        )

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

        names = self._entry_names()
        for entry_name in names:
            src = self._source_for(entry_name)
            conflict = self._sync_one(src, entry_name, store, report)
            if conflict is not None:
                conflicts[entry_name] = conflict
            else:
                conflicts.pop(entry_name, None)

        self._prune(
            keep=set(names),
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
    ) -> EntryConflict | None:
        key = self._key(entry_name)
        dest = self._dest_for(entry_name)
        incoming_hash = ChecksumStore.file_hash(src)
        stored_hash = store.entries.get(key)

        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
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
            return self._park(src, entry_name, incoming_hash, change="changed")

        if incoming_hash == stored_hash:
            report.unchanged.append(entry_name)
            return None

        if local_hash == stored_hash:
            # Untouched since the last sync — take theirs without asking.
            shutil.copy2(src, dest)
            store.entries[key] = incoming_hash
            report.updated.append(entry_name)
            return None

        return self._park(src, entry_name, incoming_hash, change="changed")

    def _park(
        self, src: Path, entry_name: str, incoming_hash: str, *, change: str
    ) -> EntryConflict:
        """Put the incoming version beside the local one and ask later."""
        dest = self._dest_for(entry_name)
        incoming_path = dest.with_name(dest.name + ".incoming")
        incoming_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, incoming_path)
        return EntryConflict(
            entry_kind=self._kind,
            entry_name=entry_name,
            change=change,
            incoming_hash=incoming_hash,
            incoming_path=incoming_path,
        )

    def _prune(
        self,
        keep: set[str],
        store: ChecksumStore,
        report: GroupSyncReport,
        conflicts: dict[str, EntryConflict],
    ) -> None:
        """Remove what the group no longer ships.

        Only entries we put there and nobody has touched. A group switch
        has to actually swap them — but an edited file is the person's
        work, and deleting it because an admin moved them to another team
        would be indefensible.
        """
        prefix = f"{self._subdir}/"
        suffix = self._filename.format(name="")  # e.g. ".md" or "/SKILL.md"
        for key, stored_hash in list(store.entries.items()):
            if not key.startswith(prefix) or not key.endswith(suffix):
                continue
            entry_name = key[len(prefix) : len(key) - len(suffix)]
            if not entry_name or entry_name in keep:
                continue

            dest = self._dest_for(entry_name)
            if not dest.exists():
                store.entries.pop(key, None)
                continue

            if ChecksumStore.file_hash(dest) == stored_hash:
                dest.unlink()
                # A skill is a directory; take it with the file, but
                # only if nothing else of the person's is in there.
                parent = dest.parent
                if parent != self.dest_dir and not any(parent.iterdir()):
                    parent.rmdir()
                store.entries.pop(key, None)
                report.removed.append(entry_name)
                conflicts.pop(entry_name, None)
            else:
                conflicts[entry_name] = EntryConflict(
                    entry_kind=self._kind,
                    entry_name=entry_name,
                    change="removed",
                    incoming_hash=stored_hash,
                )

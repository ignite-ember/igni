"""Which artifact project this working copy belongs to.

Documents on the server belong to a project, and every write and
search from this session carries its id — otherwise a developer with
documents in two repositories gets both mixed into one search, and then
cannot open half of them, because the local graph is per project.

**Found by remote, remembered locally.** The server keeps a table of
every remote each project has been seen under (see igni__artifacts,
``artifact_project_remotes``). This sends the working copy's ``origin``
and the project it resolved to last time, and gets one project back.
The id is remembered in the clone's own git config (``igni.project``):
never committed, private to this clone, and untouched by a changed
remote or a moved folder — which is exactly what lets a changed remote
be recognised as the same project rather than a new one. A folder that
is not a git repository keeps it in ``.igni/project-id`` instead.

**No listener, a cheap check.** Nothing in git announces that a remote
changed — no hook fires on ``git remote set-url``, and the config can be
edited by hand. So before every artifact call this compares the
modification time of the git config with the one it last resolved
against. One ``stat`` when nothing moved; a fresh resolution when
something did. A change is therefore picked up on the very next call
that would have used the old answer, never later.

What the server answered is kept as :attr:`WorkingCopyProject.notice`
when it is worth telling somebody: a project created, a remote attached
because it changed, or a remote that already belongs to another
project (kept, not switched — see the server's rules).
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ember_code.core.paths import CONFIG_DIR

logger = logging.getLogger(__name__)

#: The key in the clone's git config that remembers the project.
PIN_KEY = "igni.project"
#: Where a folder that is not a git repository remembers it.
PIN_FILE = Path(CONFIG_DIR) / "project-id"
#: How long a failed resolution is trusted before it is tried again,
#: so an unreachable server is not asked on every single call.
RETRY_AFTER_SECONDS = 60.0

Resolve = Callable[..., Awaitable[dict[str, Any] | None]]


@dataclass(frozen=True)
class ProjectBinding:
    """The project this working copy resolved to, and how."""

    project_id: str
    name: str
    status: str
    remotes: list[str] = field(default_factory=list)
    remote_key: str = ""
    conflicting_project_id: str = ""
    conflicting_name: str = ""

    @classmethod
    def from_server(cls, body: dict[str, Any]) -> ProjectBinding:
        return cls(
            project_id=str(body.get("project_id") or ""),
            name=str(body.get("name") or ""),
            status=str(body.get("status") or ""),
            remotes=[str(r) for r in body.get("remotes") or []],
            remote_key=str(body.get("remote_key") or ""),
            conflicting_project_id=str(body.get("conflicting_project_id") or ""),
            conflicting_name=str(body.get("conflicting_name") or ""),
        )

    @property
    def notice(self) -> str:
        """One line for a person, or empty when nothing happened worth saying."""
        if self.status == "created":
            where = f" for {self.remote_key}" if self.remote_key else ""
            return f"Started the document project '{self.name}'{where}."
        if self.status == "attached":
            return (
                f"This repository's remote is now {self.remote_key}; its documents stay in "
                f"'{self.name}'. The old remote keeps working for older clones."
            )
        if self.status == "conflict":
            return (
                f"The remote {self.remote_key} belongs to the document project "
                f"'{self.conflicting_name}', but this working copy uses '{self.name}'. "
                f"Still using '{self.name}'. Run `/artifacts project link` to move the remote "
                f"here, or `/artifacts project use {self.conflicting_project_id}` to switch."
            )
        return ""


class WorkingCopyProject:
    """Resolves and remembers this working copy's project, re-checking cheaply."""

    def __init__(self, project_dir: str | Path, resolve: Resolve) -> None:
        self._dir = Path(project_dir)
        self._resolve = resolve
        self._lock = asyncio.Lock()
        self._binding: ProjectBinding | None = None
        self._stamp: tuple[int, int] | None = None
        self._failed_at: float = 0.0
        self._git_config: Path | None | bool = False  # False = not looked up yet
        #: The last thing worth telling somebody, until it is read.
        self.notice: str = ""

    # ── the one call everything makes ──────────────────────────────────

    async def project_id(self) -> str | None:
        binding = await self.binding()
        return binding.project_id if binding else None

    async def binding(self, *, force: bool = False) -> ProjectBinding | None:
        """The current project; re-resolved only if the working copy changed.

        ``force`` re-resolves regardless — for a wake, or a command
        that just changed the pin.
        """
        stamp = await asyncio.to_thread(self._current_stamp)
        if not force and self._binding is not None and stamp == self._stamp:
            return self._binding
        if (
            not force
            and self._binding is None
            and stamp == self._stamp
            and time.monotonic() - self._failed_at < RETRY_AFTER_SECONDS
        ):
            return None
        async with self._lock:
            if not force and self._binding is not None and stamp == self._stamp:
                return self._binding
            return await self._resolve_now()

    def invalidate(self) -> None:
        """Forget the cached answer; the next call resolves again."""
        self._stamp = None
        self._failed_at = 0.0

    # ── explicit choices ───────────────────────────────────────────────

    async def use(self, project_id: str) -> ProjectBinding | None:
        """Remember ``project_id`` for this working copy and resolve with it."""
        await asyncio.to_thread(self._write_pin, project_id)
        return await self.binding(force=True)

    def remote(self) -> str | None:
        """The working copy's ``origin`` URL, or None."""
        return self._git("remote", "get-url", "origin")

    # ── internals ──────────────────────────────────────────────────────

    async def _resolve_now(self) -> ProjectBinding | None:
        remote, pin, name = await asyncio.to_thread(self._read_working_copy)
        try:
            body = await self._resolve(remote=remote, remembered_project_id=pin, name=name)
        except Exception as exc:  # noqa: BLE001 — unscoped is a fallback, not a crash
            logger.info(
                "artifact project: could not resolve (%s); searching unscoped", type(exc).__name__
            )
            body = None
        if not body or not body.get("project_id"):
            self._binding = None
            self._failed_at = time.monotonic()
            self._stamp = await asyncio.to_thread(self._current_stamp)
            return None

        binding = ProjectBinding.from_server(body)
        if binding.project_id != pin:
            await asyncio.to_thread(self._write_pin, binding.project_id)
        # After the pin write, which itself changes the config's mtime —
        # stamping before it would make the next call resolve again.
        self._stamp = await asyncio.to_thread(self._current_stamp)
        self._binding = binding
        if binding.notice:
            self.notice = binding.notice
            logger.info("artifact project: %s", binding.notice)
        return binding

    def _read_working_copy(self) -> tuple[str | None, str | None, str]:
        top = self._git("rev-parse", "--show-toplevel")
        name = Path(top).name if top else self._dir.resolve().name
        return self.remote(), self._read_pin(), name

    def _read_pin(self) -> str | None:
        if self._config_path() is not None:
            return self._git("config", "--local", "--get", PIN_KEY)
        path = self._dir / PIN_FILE
        try:
            return path.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def _write_pin(self, project_id: str) -> None:
        if self._config_path() is not None:
            self._git("config", "--local", PIN_KEY, project_id)
            return
        path = self._dir / PIN_FILE
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(project_id + "\n", encoding="utf-8")
        except OSError as exc:
            logger.info("artifact project: could not remember %s (%s)", project_id, exc)

    def _config_path(self) -> Path | None:
        """The clone's git config — shared by every worktree of it — or None."""
        if self._git_config is False:
            common = self._git("rev-parse", "--git-common-dir")
            if common:
                path = Path(common)
                if not path.is_absolute():
                    path = (self._dir / path).resolve()
                self._git_config = path / "config"
            else:
                self._git_config = None
        return self._git_config or None  # type: ignore[return-value]

    def _current_stamp(self) -> tuple[int, int]:
        """What changes when the remote or the pin could have: config and pin-file mtimes."""

        def mtime(path: Path | None) -> int:
            try:
                return os.stat(path).st_mtime_ns if path else 0
            except OSError:
                return 0

        return mtime(self._config_path()), mtime(self._dir / PIN_FILE)

    def _git(self, *args: str) -> str | None:
        try:
            done = subprocess.run(
                ["git", *args],
                cwd=str(self._dir),
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        out = done.stdout.strip()
        return out if done.returncode == 0 and out else None

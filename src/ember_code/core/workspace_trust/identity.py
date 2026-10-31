"""Which folder this is: its canonical path and its git remote.

A trust decision is recorded against both. The path alone would carry
trust over to whatever is later checked out or copied to the same place —
a different repository at a trusted path would inherit it. The remote
alone would trust every clone of a repository wherever it lands, including
a stale copy in ``~/Downloads``. Together, a moved or renamed clone asks
again, and so does a different repository at the same path.

The remote is read from ``.git/config`` directly, never by running ``git``:
the folder is not trusted yet, and a repository's own config can make
``git`` run commands (``core.fsmonitor``, ``core.sshCommand`` …).
"""

from __future__ import annotations

import contextlib
import fnmatch
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from ember_code.core.data_paths import canonical_project_dir

#: ``[remote "origin"]`` — a git config section header naming a remote.
_REMOTE_SECTION_RE = re.compile(r'^\s*\[\s*remote\s+"([^"]+)"\s*\]\s*$')
_ANY_SECTION_RE = re.compile(r"^\s*\[")
_URL_RE = re.compile(r"^\s*url\s*=\s*(.+?)\s*$", re.IGNORECASE)
#: ``git@host:owner/repo`` — scp-like syntax, no scheme.
_SCP_RE = re.compile(r"^(?:[^@/]+@)?([^:/]+):(?!//)(.+)$")
_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*)://(.*)$")


def canonical_remote(url: str) -> str:
    """``host/owner/repo`` for any spelling of a remote, credentials removed.

    ``git@github.com:acme/app.git``, ``ssh://git@github.com/acme/app`` and
    ``https://user:token@github.com/acme/app.git/`` all become
    ``github.com/acme/app``. The host is lower-cased; the path is not (some
    servers are case-sensitive). A local path is returned as written, less a
    trailing slash. Shell wildcards pass through untouched, so a pattern is
    canonicalised by the same function as the remote it is matched against.
    """
    text = url.strip()
    if not text:
        return ""
    match = _SCHEME_RE.match(text)
    if match:
        scheme, rest = match.group(1).lower(), match.group(2)
        if scheme == "file":
            return rest.rstrip("/")
        authority, _, path = rest.partition("/")
        host = authority.rsplit("@", 1)[-1]  # drop ``user[:password]@``
        host = host.split(":", 1)[0].lower()  # drop the port
        return _join(host, path)
    scp = _SCP_RE.match(text)
    if scp and not text.startswith(("/", ".", "~")):
        return _join(scp.group(1).lower(), scp.group(2))
    if text.startswith(("/", ".", "~")):
        return text.rstrip("/")
    # Already ``host/path`` (how a pattern is usually written).
    host, _, path = text.partition("/")
    return _join(host.lower(), path)


def _join(host: str, path: str) -> str:
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    return f"{host}/{path}" if path else host


def remote_matches(remote: str | None, patterns: list[str]) -> bool:
    """Whether ``remote`` matches one of ``patterns`` (shell wildcards)."""
    if not remote:
        return False
    target = canonical_remote(remote)
    return any(
        fnmatch.fnmatchcase(target, canonical_remote(pattern)) for pattern in patterns if pattern
    )


class WorkspaceIdentity(BaseModel):
    """A folder as the trust store knows it."""

    model_config = ConfigDict(frozen=True)

    #: Canonical absolute path: ``~`` expanded, symlinks resolved.
    path: str
    #: The repository's remote, canonical (:func:`canonical_remote`), or
    #: ``None`` for a folder that is not in a git repository or has no remote.
    remote: str | None = None

    @classmethod
    def of(cls, project_dir: str | Path) -> WorkspaceIdentity:
        folder = canonical_project_dir(project_dir)
        raw = GitConfigRemote.read(folder)
        return cls(path=str(folder), remote=canonical_remote(raw) if raw else None)

    @property
    def key(self) -> str:
        return f"{self.path}\n{self.remote or ''}"


class GitConfigRemote:
    """The remote URL of the repository containing a folder, from its config file."""

    #: Remotes tried first, in order; otherwise the first one the config names.
    PREFERRED: tuple[str, ...] = ("origin", "upstream")

    @classmethod
    def read(cls, folder: Path) -> str | None:
        config = cls._config_file(folder)
        if config is None:
            return None
        try:
            text = config.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        remotes = cls._remotes(text)
        if not remotes:
            return None
        for name in cls.PREFERRED:
            if name in remotes:
                return remotes[name]
        return next(iter(remotes.values()))

    @staticmethod
    def _remotes(text: str) -> dict[str, str]:
        remotes: dict[str, str] = {}
        current: str | None = None
        for line in text.splitlines():
            section = _REMOTE_SECTION_RE.match(line)
            if section:
                current = section.group(1)
                continue
            if _ANY_SECTION_RE.match(line):
                current = None
                continue
            if current is not None and current not in remotes:
                url = _URL_RE.match(line)
                if url:
                    remotes[current] = url.group(1).strip().strip('"')
        return remotes

    @staticmethod
    def _config_file(folder: Path) -> Path | None:
        """``<git dir>/config`` for the repository ``folder`` is in, walking up."""
        for candidate in (folder, *folder.parents):
            dot_git = candidate / ".git"
            if dot_git.is_dir():
                return dot_git / "config"
            if dot_git.is_file():
                # A worktree or submodule: ``gitdir: <path>``, and a worktree's
                # git dir names the shared one in ``commondir``.
                try:
                    first = dot_git.read_text(encoding="utf-8").strip().splitlines()[0]
                except (OSError, IndexError):
                    return None
                if not first.startswith("gitdir:"):
                    return None
                git_dir = Path(first[len("gitdir:") :].strip())
                if not git_dir.is_absolute():
                    git_dir = (candidate / git_dir).resolve()
                common = git_dir / "commondir"
                if common.is_file():
                    with contextlib.suppress(OSError):
                        git_dir = (git_dir / common.read_text().strip()).resolve()
                return git_dir / "config"
        return None


__all__ = ["GitConfigRemote", "WorkspaceIdentity", "canonical_remote", "remote_matches"]

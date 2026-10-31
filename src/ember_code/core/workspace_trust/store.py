"""The person's trust decisions, kept in their own data directory.

``~/.igni/workspace-trust.json`` — never in the repository, which is the
thing being trusted: a decision a folder could write for itself would be no
decision at all. One entry per folder, keyed by its canonical path *and* its
git remote (:class:`~ember_code.core.workspace_trust.identity.WorkspaceIdentity`),
so a moved clone or a different repository at a trusted path is unset and
asks again.

The path is fixed rather than following ``storage.data_dir``: the decision
is needed while settings are loaded (a folder's config is only merged once
it is trusted), before ``storage.data_dir`` is known.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from ember_code.core.paths import CONFIG_DIR
from ember_code.core.workspace_trust.identity import WorkspaceIdentity

logger = logging.getLogger(__name__)

TrustState = Literal["trusted", "untrusted"]

#: The file's name under the user's data directory.
STORE_FILE = "workspace-trust.json"


class TrustEntry(BaseModel):
    path: str
    remote: str | None = None
    state: TrustState
    decided_at: str = ""

    @property
    def identity(self) -> WorkspaceIdentity:
        return WorkspaceIdentity(path=self.path, remote=self.remote)


class _TrustFile(BaseModel):
    version: int = 1
    workspaces: list[TrustEntry] = Field(default_factory=list)


class WorkspaceTrustStore:
    """Read and write the per-user trust file.

    Every read goes to disk: ``igni trust`` in a terminal and the desktop's
    backend are different processes, and a revocation in one must reach the
    other on its next decision. The file is small and read a handful of times
    per session start.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._explicit_path = path
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        # Resolved per call so a test's (or a process's) ``HOME`` is honoured.
        return self._explicit_path or (Path.home() / CONFIG_DIR / STORE_FILE)

    def get(self, identity: WorkspaceIdentity) -> TrustState | None:
        for entry in self._load().workspaces:
            if entry.path == identity.path and entry.remote == identity.remote:
                return entry.state
        return None

    def set(self, identity: WorkspaceIdentity, state: TrustState) -> None:
        with self._lock:
            data = self._load()
            kept = [e for e in data.workspaces if e.path != identity.path]
            kept.append(
                TrustEntry(
                    path=identity.path,
                    remote=identity.remote,
                    state=state,
                    decided_at=datetime.now(UTC).isoformat(timespec="seconds"),
                )
            )
            data.workspaces = sorted(kept, key=lambda e: e.path)
            self._save(data)

    def forget(self, identity: WorkspaceIdentity) -> bool:
        """Drop every decision recorded for ``identity.path``. True if one existed."""
        with self._lock:
            data = self._load()
            kept = [e for e in data.workspaces if e.path != identity.path]
            if len(kept) == len(data.workspaces):
                return False
            data.workspaces = kept
            self._save(data)
            return True

    def entries(self) -> list[TrustEntry]:
        return list(self._load().workspaces)

    def _load(self) -> _TrustFile:
        path = self.path
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return _TrustFile()
        except OSError as exc:
            logger.warning("Could not read %s: %s; treating every folder as undecided.", path, exc)
            return _TrustFile()
        try:
            return _TrustFile.model_validate(json.loads(raw))
        except (json.JSONDecodeError, ValidationError) as exc:
            # Fails closed: an unreadable file trusts nothing.
            logger.warning("%s is not a valid trust file (%s); ignoring it.", path, exc)
            return _TrustFile()

    def _save(self, data: _TrustFile) -> None:
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(data.model_dump_json(indent=2) + "\n", encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)


__all__ = ["STORE_FILE", "TrustEntry", "TrustState", "WorkspaceTrustStore"]

"""Typed schemas for the per-project backend discovery lockfile.

Extracted from :mod:`ember_code.backend.lockfile` — the previous
module returned raw ``dict[str, Any]`` from ``Lockfile.read`` /
``discover`` and signalled version-mismatch by smuggling a magic
``"_version_mismatch": True`` key back through the same dict shape.
Every payload / result / discovery outcome that crosses the module
boundary now lives here as a Pydantic model so mypy + Ruff +
Pydantic validation give schema coverage at every seam.

Sibling convention: mirrors :mod:`schemas_lifecycle` /
:mod:`schemas_history` — one schemas module per top-level domain,
per :file:`CODE_STANDARDS.md` Rule 1 (no raw dicts at seams) +
Pattern 2 (no sentinel keys — use a discriminated union) +
Pattern 3 (expected failures returned as values, not raised).

Consumers:

* :class:`LockfilePayload` — the on-disk JSON payload. Fields
  ``pid`` / ``port`` / ``wire_version`` / ``created_at`` map to
  exactly the keys the FE and out-of-tree readers already consume;
  :meth:`model_dump` produces byte-identical JSON to the previous
  hand-rolled dict, so the on-disk wire format is stable.
* :class:`DiscoveryOutcome` — string tag identifying which arm of
  the discovery result was hit.
* :class:`LiveBackend` / :class:`NoBackend` / :class:`VersionMismatch`
  — the three real outcomes of a discovery attempt. Replaces the
  previous ``dict | None | dict-with-magic-key`` triple.
* :class:`DiscoveryResult` — the tagged union of the three, with
  ``status`` as the Pydantic discriminator. Callers ``match`` on
  ``.status`` or use :func:`isinstance` — the old
  ``result.get("_version_mismatch")`` smuggle key is gone.
* :class:`WriteLockfileResult` / :class:`RemoveLockfileResult` —
  result-envelope models replacing raised ``OSError`` from
  ``Lockfile.write`` / ``.remove``. Expected failures (permissions,
  missing parent, non-existent file) become observable outcomes,
  not exceptions the caller has to remember to swallow.
"""

from __future__ import annotations

import os
import socket
import time
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, Field


class LockfilePayload(BaseModel):
    """On-disk lockfile contents — the JSON payload stored at
    ``<project>/.ember/backend.lock``.

    Field order and names are fixed by the on-disk wire format —
    out-of-tree readers (VSCode extension, JB plugin, Tauri shell,
    plain ``jq``) parse this file directly, so the key-set MUST NOT
    drift. :meth:`model_dump` produces exactly
    ``{"pid", "port", "wire_version", "created_at"}`` in insertion
    order to match the previous hand-rolled dict serialisation.

    ``created_at`` lets a future GC step (``ember doctor --clean``)
    prune ancient locks whose BEs have long since died without
    cleaning up.
    """

    pid: int
    port: int
    wire_version: str
    created_at: int

    @classmethod
    def now(cls, *, pid: int, port: int, wire_version: str) -> LockfilePayload:
        """Build a payload stamped with the current wall-clock time.

        Centralises the ``int(time.time())`` call so
        :class:`Lockfile` never touches the time source directly —
        tests that need deterministic ``created_at`` values build
        the payload explicitly and pass it in.
        """
        return cls(
            pid=pid,
            port=port,
            wire_version=wire_version,
            created_at=int(time.time()),
        )

    def matches_version(self, expected: str) -> bool:
        """Exact-string wire-version comparison. Patch-level drift
        counts as incompatible — we prefer a false-alarm 'spawn new'
        over a false-clear 'protocol drift silently corrupts state'."""
        return self.wire_version == expected

    def age_seconds(self, *, now: float | None = None) -> int:
        """Seconds since :attr:`created_at`. ``now`` is injectable
        so tests can pin the clock without patching ``time.time``."""
        current = time.time() if now is None else now
        return max(0, int(current) - self.created_at)

    def is_pid_alive(self) -> bool:
        """Cross-POSIX check: is :attr:`pid` still a live process?

        ``os.kill(pid, 0)`` is the canonical no-signal probe —
        succeeds if the process exists, ``ProcessLookupError`` if
        it's gone, ``PermissionError`` if it exists but we can't
        signal it (which still means alive for our purposes: same
        user's own processes always allow signal 0).
        """
        if self.pid <= 0:
            return False
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def is_port_reachable(self, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
        """Cheap TCP connect probe — :attr:`port` is reachable if
        the ``connect`` call completes without raising. We don't
        hand-shake at the WebSocket layer here; that would require
        an async client and add complexity. The :meth:`is_pid_alive`
        check already tells us the process is alive; the TCP probe
        just confirms it's still listening.
        """
        try:
            with socket.create_connection((host, self.port), timeout=timeout):
                return True
        except OSError:
            return False


class DiscoveryOutcome(str, Enum):
    """Discriminator tag for :class:`DiscoveryResult` union arms."""

    LIVE = "live"
    NONE = "none"
    VERSION_MISMATCH = "version_mismatch"


class LiveBackend(BaseModel):
    """A live, healthy, version-matched BE was discovered — the
    caller should connect to ``payload.port`` instead of spawning
    a duplicate BE for the same project."""

    status: Literal[DiscoveryOutcome.LIVE] = DiscoveryOutcome.LIVE
    payload: LockfilePayload


class NoBackend(BaseModel):
    """No usable BE was found — either no lockfile, or a stale one
    that was cleaned up. The caller should spawn a fresh BE."""

    status: Literal[DiscoveryOutcome.NONE] = DiscoveryOutcome.NONE


class VersionMismatch(BaseModel):
    """A live BE exists but its wire version doesn't match. The
    lockfile is intact (a legitimate different-version client owns
    it); the caller should surface a user-facing notification
    instead of forcing a duplicate spawn.

    Replaces the previous ``{**data, "_version_mismatch": True}``
    magic-key sentinel — the smuggle key is gone; the discriminator
    (``status``) carries the signal.
    """

    status: Literal[DiscoveryOutcome.VERSION_MISMATCH] = DiscoveryOutcome.VERSION_MISMATCH
    payload: LockfilePayload
    expected: str


#: Discriminated union of every outcome a discovery attempt can
#: produce. Callers switch on ``.status`` (or :func:`isinstance`)
#: rather than probing dict keys.
DiscoveryResult = Annotated[
    LiveBackend | NoBackend | VersionMismatch,
    Field(discriminator="status"),
]


class WriteLockfileResult(BaseModel):
    """Outcome of :meth:`Lockfile.write`.

    Replaces the previous "raise ``OSError`` and hope the caller
    remembered a ``try/except``" contract. Expected write failures
    (permissions, missing ``.ember`` dir the caller can't create,
    read-only filesystem) surface as ``ok=False`` with a
    human-readable :attr:`reason`. Unexpected exceptions still
    propagate — the envelope covers *expected* failures, not
    programming bugs.
    """

    ok: bool
    reason: str | None = None
    payload: LockfilePayload | None = None


class RemoveLockfileResult(BaseModel):
    """Outcome of :meth:`Lockfile.remove`.

    ``existed`` distinguishes 'no file to remove' (idempotent
    success) from 'file existed and got removed'. ``reason`` carries
    the human-readable error string on ``ok=False`` (e.g. permission
    denied on the parent directory).
    """

    ok: bool
    existed: bool = False
    reason: str | None = None

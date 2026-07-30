"""Pydantic wire/data models for the shell-orphan subsystem.

Extracted from :mod:`shell_orphan` per the sibling schemas
convention (mirrors :mod:`schemas_processes` / :mod:`schemas_lifecycle`
in the backend package). Every wire / result model this subsystem
hands across a module boundary lives here so Rule 1 stays
discoverable at one path.

Consumers:

* :class:`OrphanProcStub` — two-field stand-in for
  :class:`asyncio.subprocess.Process`. Formerly a ``@dataclass`` —
  promoted to Pydantic (Rule 1) so validation catches a caller
  handing in a str pid, etc.
* :class:`OrphanReadResult` — typed return for the "read a tail
  of an orphan's log" helper. Callers get ``(content,
  is_placeholder)`` instead of string-sniffing the placeholder
  substring. Kept as an internal helper (see the "read()
  polymorphic contract" note in :class:`OrphanProcess`) — the
  primary :meth:`OrphanProcess.read` still returns ``str`` so the
  polymorphic call site
  (``mp.read(tail=tail)``) in
  :meth:`ProcessesController.read_tail` stays uniform with
  :class:`ManagedProcess.read`.
* :class:`RehydrateResult` — typed replacement for the bare
  ``int`` returned by the old procedural
  ``rehydrate_orphan_processes``. Populates ``reason`` on any of
  the three failure branches (store-init / list_all / per-row
  remove) so :class:`RehydrateController.orphan_processes` can
  plumb the reason through to
  :class:`~ember_code.backend.schemas_lifecycle.RehydrateOutcome`
  instead of swallowing at DEBUG.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class OrphanProcStub(BaseModel):
    """Two-field stub matching the bits of
    :class:`asyncio.subprocess.Process` the registry / RPCs read on
    a real :class:`ManagedProcess`. Kept as a Pydantic model
    (Rule 1) so validation catches a caller handing in a str pid.
    """

    pid: int
    returncode: int | None = None


class OrphanReadResult(BaseModel):
    """Typed return for an orphan-log tail read.

    Internal helper — the primary :meth:`OrphanProcess.read`
    method returns ``str`` (unchanged) so the polymorphic
    contract with :meth:`ManagedProcess.read` stays consistent.
    Callers that need the placeholder-vs-real distinction reach
    for this via a dedicated helper on :class:`OrphanProcess`.

    TODO(refactor): unify with :class:`ManagedProcess.read` in a
    follow-up so both sides return a typed result — that lets
    :meth:`ProcessesController.read_tail` distinguish placeholder
    from real content on the wire.
    """

    content: str
    is_placeholder: bool = False


class RehydrateResult(BaseModel):
    """Typed outcome of :meth:`OrphanRehydrator.run`.

    Replaces the bare ``int`` returned by the previous procedural
    ``rehydrate_orphan_processes``. Callers plumb ``reason``
    straight through to
    :class:`~ember_code.backend.schemas_lifecycle.RehydrateOutcome`
    so the three failure branches (store init, list_all, per-row
    remove) become observable at INFO level in the boot summary.

    * ``ok`` — ``True`` iff the pass completed without an
      unhandled exception. A zero-surfaced no-op is still
      ``ok=True``.
    * ``surfaced`` — number of alive orphans injected into the
      registry.
    * ``pruned`` — number of dead rows removed from the store.
    * ``reason`` — short label + exception message on failure, or
      an empty string on success.
    """

    ok: bool = True
    surfaced: int = 0
    pruned: int = 0
    reason: str = Field(default="")

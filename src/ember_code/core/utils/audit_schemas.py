"""Pydantic DTOs consumed by :class:`AuditLogger`.

Kept in a sibling module (``audit_schemas.py``) so:

* The wire shape of an audit-log line lives on the data, not on
  the IO orchestrator. A future non-file sink (JSON transport,
  syslog forwarder, structured-logging bus) can reuse
  :class:`AuditEntry` + :meth:`AuditEntry.to_jsonl_line` without
  importing :class:`AuditLogger`.
* Rule 1 (no raw dicts crossing module boundaries) is enforced
  structurally: :class:`AuditLogger` accepts a single typed
  :class:`AuditEntry` instead of five positional / keyword args
  plus a ``details: dict[str, Any] | None`` bag. The four call
  sites construct the entry via named factory classmethods
  (``AuditEntry.success`` / ``.error`` / ``.blocked`` /
  ``.tool_call``) so the ``status`` / ``details`` fan-out lives
  on the model, not on the caller.
* Pattern 3 (Result-envelope returns) replaces the pre-refactor
  silent ``except OSError: pass``. :meth:`AuditLogger.log`
  returns an :class:`AuditWriteResult` — callers can inspect a
  full-disk / read-only-mount failure but need not (audit is
  fire-and-forget by design).

Every model here is re-exported from
:mod:`ember_code.core.utils.audit` so external callers keep a
single import site — matches the ``update_checker.py`` /
``display.py`` re-export convention.

Wire-format note (breaking change vs. pre-refactor):
:class:`AuditStatus` normalises ``status`` values to lowercase
strings — ``"blocked"`` where the previous
:meth:`AuditLogger.log_blocked` wrote ``"BLOCKED"``. This is the
intended fix (the docstring on the pre-refactor ``log()`` method
already listed the enum as ``success / error / blocked``); any
downstream log-scraper needs to accept both cases during the
transition window.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field


class AuditStatus(str, Enum):
    """Typed status value for an audit entry.

    ``str, Enum`` (StrEnum-shaped) so the wire value stays a
    plain string (``AuditEntry.status`` serialises to
    ``"success"`` etc.) while the code path has an enum to switch
    on. Fixes the pre-refactor casing inconsistency where
    :meth:`AuditLogger.log_blocked` wrote ``"BLOCKED"`` uppercase
    while :meth:`log` accepted ``"success"`` / ``"error"``
    lowercase.
    """

    SUCCESS = "success"
    ERROR = "error"
    BLOCKED = "blocked"


# ── Discriminated details union ──────────────────────────────────


class SuccessDetails(BaseModel):
    """Details variant for the happy path.

    Empty payload — the ``status`` field on :class:`AuditEntry`
    already carries the signal. Kept as a distinct type so the
    discriminated union has a symmetric shape and the caller can
    write ``AuditEntry.success(...)`` without threading a
    ``details=None`` sentinel through.
    """

    kind: Literal["success"] = "success"


class ErrorDetails(BaseModel):
    """Details variant for a tool / turn failure.

    Carries the stringified exception in :attr:`error`. Kept as a
    string (not a raw ``Exception`` object) because the audit log
    is a wire format — an ``Exception`` object would not
    round-trip through JSON.
    """

    kind: Literal["error"] = "error"
    error: str
    error_type: str | None = None


class BlockedDetails(BaseModel):
    """Details variant for a policy-blocked tool call.

    Replaces the pre-refactor ``details={"reason": ...}`` dict
    literal at every call site. ``reason`` is required — a
    blocked-without-a-reason entry is a logic bug in the caller.
    """

    kind: Literal["blocked"] = "blocked"
    reason: str


class ToolCallDetails(BaseModel):
    """Details variant for a skill / tool invocation with args.

    :attr:`args` is the raw arg string as typed by the user
    after the ``/skill-name`` prefix (e.g. ``"foo --bar 3"``);
    the audit log captures it verbatim for post-hoc
    reproduction of the invocation. Kept typed as ``str`` (not
    ``dict[str, Any]``) because that's the actual shape at the
    one call site — the pre-refactor ``details={"args": args}``
    dict-wrapper was scaffolding for the untyped ``details``
    bag, not a real structural need.
    """

    kind: Literal["tool_call"] = "tool_call"
    args: str = ""


AuditDetails = Annotated[
    SuccessDetails | ErrorDetails | BlockedDetails | ToolCallDetails,
    Field(discriminator="kind"),
]


# ── Entry + write result ────────────────────────────────────────


class AuditEntry(BaseModel):
    """One typed row of the audit log.

    Constructed via the ``AuditEntry.success`` / ``.error`` /
    ``.blocked`` / ``.tool_call`` factory classmethods — the
    :class:`AuditStatus` value and the matching
    :class:`AuditDetails` variant travel together, so a caller
    can't accidentally construct an ``AuditStatus.BLOCKED`` entry
    with :class:`ErrorDetails` payload.

    :meth:`to_jsonl_line` owns the wire format. The nested
    ``kind`` discriminator field is stripped at serialisation so
    the on-disk JSONL stays byte-compatible with the pre-refactor
    format (no new keys downstream consumers weren't parsing).
    """

    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    session_id: str
    agent: str
    tool: str
    status: AuditStatus
    details: AuditDetails | None = None

    # ── Factory classmethods ─────────────────────────────────────

    @classmethod
    def success(cls, *, session_id: str, agent_name: str, tool_name: str) -> AuditEntry:
        """Build a success entry with no details payload."""
        return cls(
            session_id=session_id,
            agent=agent_name,
            tool=tool_name,
            status=AuditStatus.SUCCESS,
            details=None,
        )

    @classmethod
    def error(
        cls,
        *,
        session_id: str,
        agent_name: str,
        tool_name: str,
        error: str,
        error_type: str | None = None,
    ) -> AuditEntry:
        """Build an error entry carrying the stringified exception."""
        return cls(
            session_id=session_id,
            agent=agent_name,
            tool=tool_name,
            status=AuditStatus.ERROR,
            details=ErrorDetails(error=error, error_type=error_type),
        )

    @classmethod
    def blocked(
        cls,
        *,
        session_id: str,
        agent_name: str,
        tool_name: str,
        reason: str,
    ) -> AuditEntry:
        """Build a blocked-by-policy entry."""
        return cls(
            session_id=session_id,
            agent=agent_name,
            tool=tool_name,
            status=AuditStatus.BLOCKED,
            details=BlockedDetails(reason=reason),
        )

    @classmethod
    def tool_call(
        cls,
        *,
        session_id: str,
        agent_name: str,
        tool_name: str,
        args: str,
    ) -> AuditEntry:
        """Build a tool-invocation entry with a captured arg string.

        Status is :attr:`AuditStatus.SUCCESS` — the entry records
        that the call was dispatched. Failures land through
        :meth:`error` on the exception path.
        """
        return cls(
            session_id=session_id,
            agent=agent_name,
            tool=tool_name,
            status=AuditStatus.SUCCESS,
            details=ToolCallDetails(args=args),
        )

    # ── Wire format ──────────────────────────────────────────────

    def to_jsonl_line(self) -> str:
        """Serialise the entry to a single newline-terminated
        JSON line.

        Wire-format guarantees:

        * ``timestamp`` renders as ISO-8601 with timezone
          (``datetime.isoformat()`` output — matches the
          pre-refactor byte-for-byte format).
        * ``details`` is omitted entirely when the entry carries
          no payload (:class:`SuccessDetails` collapses to
          absent) — preserves the pre-refactor
          "``if details: entry['details'] = details``" behaviour.
        * The nested ``kind`` discriminator is stripped so
          downstream log-scrapers don't see a new key.
        """
        payload: dict[str, Any] = {
            "timestamp": self.timestamp.isoformat(),
            "session_id": self.session_id,
            "agent": self.agent,
            "tool": self.tool,
            "status": self.status.value,
        }
        details_payload = self._render_details()
        if details_payload:
            payload["details"] = details_payload
        return json.dumps(payload) + "\n"

    def _render_details(self) -> dict[str, Any]:
        """Return the details payload without the ``kind``
        discriminator. Empty dict when there is nothing to
        record (success entries with no payload) — the caller
        drops the key entirely."""
        if self.details is None:
            return {}
        raw = self.details.model_dump(mode="json", exclude_none=True)
        raw.pop("kind", None)
        return raw


class AuditWriteResult(BaseModel):
    """Pattern-3 return envelope for :meth:`AuditLogger.log`.

    ``ok=True`` on success; ``ok=False`` with a human-readable
    :attr:`reason` on IO failure (full disk, read-only mount,
    permission denied). Callers may inspect the result but need
    not — every current call site discards the return because
    audit is fire-and-forget by design.
    """

    ok: bool
    reason: str | None = None

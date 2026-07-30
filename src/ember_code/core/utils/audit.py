"""Audit logging — records tool executions to a JSONL file.

Architecture — everything is on :class:`AuditLogger`:

* :class:`AuditLogger` — thin IO orchestrator. Constructor
  captures the log path and ensures its parent directory
  exists; :meth:`log` writes one typed :class:`AuditEntry` to
  disk and returns an :class:`AuditWriteResult` envelope.
* :class:`AuditEntry` (in the sibling schemas module) owns the
  wire shape and the factory classmethods (``success`` /
  ``error`` / ``blocked`` / ``tool_call``). :class:`AuditLogger`
  never inspects the entry's contents — the write path is a
  single :meth:`AuditEntry.to_jsonl_line` call.

Every Pydantic model in this module comes from
:mod:`.audit_schemas` and is re-exported here so external callers
keep a single import site (matches the ``update_checker.py`` /
``display.py`` re-export convention).
"""

from __future__ import annotations

import logging
from pathlib import Path

from ember_code.core.config.settings import Settings
from ember_code.core.utils.audit_schemas import (
    AuditDetails,
    AuditEntry,
    AuditStatus,
    AuditWriteResult,
    BlockedDetails,
    ErrorDetails,
    SuccessDetails,
    ToolCallDetails,
)

# Re-exports so callers keep the single-import-site convention.
__all__ = [
    "AuditDetails",
    "AuditEntry",
    "AuditLogger",
    "AuditStatus",
    "AuditWriteResult",
    "BlockedDetails",
    "ErrorDetails",
    "SuccessDetails",
    "ToolCallDetails",
]


logger = logging.getLogger(__name__)


class AuditLogger:
    """Logs tool executions to a JSON-lines file.

    Single-concern IO orchestrator:

    * Constructor captures the target path and creates the
      parent directory.
    * :meth:`log` accepts a typed :class:`AuditEntry` (built via
      its ``.success`` / ``.error`` / ``.blocked`` / ``.tool_call``
      factories at the call site), appends one JSONL line, and
      returns an :class:`AuditWriteResult` envelope.

    IO failures (full disk, read-only mount, permission denied)
    surface through the returned :class:`AuditWriteResult`; a
    warning is emitted on the first failure per instance so a
    read-only-disk session doesn't spam the log with one line per
    turn.
    """

    def __init__(self, settings: Settings) -> None:
        self._log_path = Path(settings.storage.audit_log).expanduser()
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        # First IO failure emits a warning; subsequent failures
        # are silent so a read-only-disk session doesn't spam.
        self._warned_on_write_failure = False

    @property
    def log_path(self) -> Path:
        """Absolute path to the audit-log file this instance
        writes to.
        """
        return self._log_path

    def log(self, entry: AuditEntry) -> AuditWriteResult:
        """Append one :class:`AuditEntry` to the log.

        Returns an :class:`AuditWriteResult` — ``ok=True`` on
        success, ``ok=False`` with a human-readable ``reason`` on
        IO failure. Never raises: audit is fire-and-forget by
        design; a logging failure must not break an in-flight
        tool call.
        """
        try:
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(entry.to_jsonl_line())
        except OSError as exc:
            reason = str(exc)
            if not self._warned_on_write_failure:
                logger.warning(
                    "Audit log write failed (%s); further failures in this session will be silent.",
                    reason,
                )
                self._warned_on_write_failure = True
            return AuditWriteResult(ok=False, reason=reason)
        return AuditWriteResult(ok=True)

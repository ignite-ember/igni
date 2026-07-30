"""Tests for ``core/utils/audit.AuditLogger`` — JSON-line audit
trail of tool executions.

Pins the *content* contract:

  * Each :meth:`AuditLogger.log` call appends ONE JSON-line to
    the file (newline-delimited; standard ``jq`` / log-shipper
    format).
  * Entry shape: ``{timestamp, session_id, agent, tool, status,
    details?}`` with ISO-format timestamp.
  * ``details`` is OMITTED when the entry carries no payload
    (success entries with no context) — cleaner log entries.
  * Append-only — multiple log() calls accumulate, never
    overwrite.
  * OSError is SWALLOWED into a Pattern-3
    :class:`AuditWriteResult` envelope. Load-bearing because a
    full disk or readonly file shouldn't kill the agent's tool
    execution.
  * Blocked entries use the lowercase ``"blocked"`` status —
    normalised through :class:`AuditStatus` to match the other
    statuses (``success`` / ``error``). Reason lands inside
    ``details.reason``.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from ember_code.core.utils.audit import AuditEntry, AuditLogger


def _make_logger(tmp_path: Path) -> AuditLogger:
    settings = MagicMock()
    settings.storage.audit_log = str(tmp_path / "audit.log")
    return AuditLogger(settings)


class TestEntryShape:
    def test_log_writes_one_json_line(self, tmp_path):
        # The file format is JSON-lines (newline-delimited).
        # Each log() call appends exactly one line; the file
        # must remain parseable by ``jq -c`` / ``json.loads``
        # per-line.
        logger = _make_logger(tmp_path)
        logger.log(AuditEntry.success(session_id="s1", agent_name="editor", tool_name="edit_file"))
        content = (tmp_path / "audit.log").read_text()
        lines = [ln for ln in content.splitlines() if ln]
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["session_id"] == "s1"
        assert entry["agent"] == "editor"
        assert entry["tool"] == "edit_file"
        assert entry["status"] == "success"

    def test_entry_includes_iso_timestamp(self, tmp_path):
        # Timestamps are ISO-8601 with timezone (UTC). Log-
        # shippers / downstream consumers all parse this
        # format; drift to e.g. epoch-ms would silently break
        # them.
        logger = _make_logger(tmp_path)
        logger.log(AuditEntry.success(session_id="s1", agent_name="a", tool_name="t"))
        entry = json.loads((tmp_path / "audit.log").read_text().splitlines()[0])
        ts = entry["timestamp"]
        # ISO-8601 starts with YYYY-MM-DDTHH:MM:SS — strict
        # enough to fail on epoch-ms or unix-seconds.
        assert ts[4] == "-" and ts[7] == "-" and ts[10] == "T"
        # And includes timezone (the ``+00:00`` suffix).
        assert "+" in ts or ts.endswith("Z")

    def test_details_omitted_when_success_has_no_payload(self, tmp_path):
        # Tool calls without extra context (a plain success
        # like a successful main-team turn) write a clean
        # entry with no ``details`` key at all. The
        # :class:`SuccessDetails` variant is used implicitly for
        # ``AuditEntry.success(...)`` and renders as absent.
        logger = _make_logger(tmp_path)
        logger.log(AuditEntry.success(session_id="s1", agent_name="a", tool_name="t"))
        entry = json.loads((tmp_path / "audit.log").read_text().splitlines()[0])
        assert "details" not in entry

    def test_details_included_as_nested_dict_for_tool_call(self, tmp_path):
        # When details are set (via the ``.tool_call`` factory
        # here), they land as a nested object — NOT flattened
        # onto the top-level entry (would collide with the
        # canonical keys like ``status``).
        logger = _make_logger(tmp_path)
        logger.log(
            AuditEntry.tool_call(session_id="s1", agent_name="a", tool_name="t", args="foo bar")
        )
        entry = json.loads((tmp_path / "audit.log").read_text().splitlines()[0])
        # ``kind`` discriminator is stripped from the wire
        # format so downstream log-scrapers see only the
        # original ``args`` key.
        assert entry["details"] == {"args": "foo bar"}

    def test_success_factory_writes_success_status(self, tmp_path):
        # The ``.success`` factory pins the status to
        # :attr:`AuditStatus.SUCCESS` — the wire value is
        # ``"success"``. Match the pre-refactor default.
        logger = _make_logger(tmp_path)
        logger.log(AuditEntry.success(session_id="s1", agent_name="a", tool_name="t"))
        entry = json.loads((tmp_path / "audit.log").read_text().splitlines()[0])
        assert entry["status"] == "success"


class TestAppendBehaviour:
    def test_multiple_logs_accumulate(self, tmp_path):
        # The whole point of an audit log is the append history.
        # Calling log() N times must produce N lines, never
        # overwrite. Pin via the file's line count.
        logger = _make_logger(tmp_path)
        for i in range(5):
            logger.log(
                AuditEntry.success(session_id=f"s{i}", agent_name="agent", tool_name=f"tool{i}")
            )
        lines = (tmp_path / "audit.log").read_text().splitlines()
        assert len([ln for ln in lines if ln]) == 5

    def test_preserves_existing_content_on_construct(self, tmp_path):
        # Constructing a new logger over an existing file must
        # NOT truncate. The constructor's mkdir() is the only
        # filesystem side-effect — open is "a" (append).
        path = tmp_path / "audit.log"
        path.write_text('{"pre-existing": true}\n')
        logger = _make_logger(tmp_path)
        logger.log(AuditEntry.success(session_id="s1", agent_name="a", tool_name="t"))
        lines = [ln for ln in path.read_text().splitlines() if ln]
        assert len(lines) == 2
        assert json.loads(lines[0]) == {"pre-existing": True}

    def test_parent_dir_auto_created(self, tmp_path):
        # The constructor creates the audit-log's parent dir
        # if missing (so callers don't have to mkdir
        # defensively).
        settings = MagicMock()
        settings.storage.audit_log = str(tmp_path / "deep" / "nested" / "audit.log")
        AuditLogger(settings)  # construct, no log call yet
        assert (tmp_path / "deep" / "nested").exists()


class TestOSErrorSwallow:
    """Load-bearing: logging failures must NOT break the
    session. A full disk or a read-only file mustn't crash an
    in-flight tool call. Post-refactor the failure surfaces
    through a Pattern-3 :class:`AuditWriteResult` envelope
    instead of a silent ``pass``."""

    def test_os_error_does_not_propagate(self, tmp_path):
        # The IO orchestrator catches OSError and returns an
        # ``ok=False`` envelope. A refactor that lets the
        # OSError propagate would silently start crashing
        # sessions on disk-full scenarios.
        logger = _make_logger(tmp_path)
        # Patch open() to raise OSError unconditionally.
        with patch("ember_code.core.utils.audit.open", side_effect=OSError("disk full")):
            # Must NOT raise.
            result = logger.log(AuditEntry.success(session_id="s1", agent_name="a", tool_name="t"))
        assert result.ok is False
        assert "disk full" in (result.reason or "")

    def test_write_success_returns_ok_envelope(self, tmp_path):
        # Happy path returns ``ok=True``. Pinned so a caller
        # that DOES inspect the envelope (future policy layer)
        # can rely on the invariant.
        logger = _make_logger(tmp_path)
        result = logger.log(AuditEntry.success(session_id="s1", agent_name="a", tool_name="t"))
        assert result.ok is True
        assert result.reason is None


class TestBlockedFactory:
    """``AuditEntry.blocked`` is the dedicated permission-deny
    factory. Post-refactor the status is lowercase-normalised
    (``"blocked"``) — matches the other statuses (``success`` /
    ``error``) and fixes the pre-refactor casing inconsistency."""

    def test_uses_lowercase_blocked_status(self, tmp_path):
        # The status field is the searchable signal for "find
        # all blocked tool calls". Post-refactor it's lowercase
        # to match the other :class:`AuditStatus` values;
        # downstream log-scrapers may need to accept both cases
        # during the transition window.
        logger = _make_logger(tmp_path)
        logger.log(
            AuditEntry.blocked(
                session_id="s1",
                agent_name="main",
                tool_name="run_shell",
                reason="rm -rf /",
            )
        )
        entry = json.loads((tmp_path / "audit.log").read_text().splitlines()[0])
        assert entry["status"] == "blocked"

    def test_reason_lands_in_details_dict(self, tmp_path):
        # ``reason`` is the WHY — must be greppable in the
        # audit log. Pinned in ``details.reason`` (not at top
        # level — top level reserved for canonical keys). The
        # discriminator ``kind`` is stripped from the wire
        # format.
        logger = _make_logger(tmp_path)
        logger.log(
            AuditEntry.blocked(
                session_id="s1",
                agent_name="main",
                tool_name="run_shell",
                reason="rm -rf /",
            )
        )
        entry = json.loads((tmp_path / "audit.log").read_text().splitlines()[0])
        assert entry["details"] == {"reason": "rm -rf /"}

    def test_blocked_carries_session_agent_tool(self, tmp_path):
        # The canonical fields all land — ``.blocked`` is a
        # named-factory sibling of ``.success`` / ``.error``,
        # not a totally different entry shape.
        logger = _make_logger(tmp_path)
        logger.log(
            AuditEntry.blocked(
                session_id="session-x",
                agent_name="planner",
                tool_name="edit_file",
                reason="policy",
            )
        )
        entry = json.loads((tmp_path / "audit.log").read_text().splitlines()[0])
        assert entry["session_id"] == "session-x"
        assert entry["agent"] == "planner"
        assert entry["tool"] == "edit_file"


class TestErrorFactory:
    """``AuditEntry.error`` is the dedicated failure-path
    factory. Carries the stringified exception plus the
    exception type name."""

    def test_error_status_and_details(self, tmp_path):
        logger = _make_logger(tmp_path)
        logger.log(
            AuditEntry.error(
                session_id="s1",
                agent_name="session",
                tool_name="main_team",
                error="boom",
                error_type="RuntimeError",
            )
        )
        entry = json.loads((tmp_path / "audit.log").read_text().splitlines()[0])
        assert entry["status"] == "error"
        assert entry["details"]["error"] == "boom"
        assert entry["details"]["error_type"] == "RuntimeError"

    def test_error_omits_optional_error_type(self, tmp_path):
        # ``error_type`` is optional — the pre-refactor
        # ``details={"error": ...}`` shape didn't carry it, so
        # a call site that only passes ``error=...`` must
        # produce a wire payload without ``error_type``.
        logger = _make_logger(tmp_path)
        logger.log(
            AuditEntry.error(
                session_id="s1",
                agent_name="a",
                tool_name="t",
                error="boom",
            )
        )
        entry = json.loads((tmp_path / "audit.log").read_text().splitlines()[0])
        assert entry["details"] == {"error": "boom"}

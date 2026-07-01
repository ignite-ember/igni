"""Tests for ``core/utils/audit.AuditLogger`` — JSON-line audit
trail of tool executions.

Existing tests in ``test_onboarding_and_audit.py`` are smoke
level ("doesn't crash"). This file pins the *content* contract:

  * Each log() call appends ONE JSON-line to the file (newline-
    delimited; standard ``jq`` / log-shipper format).
  * Entry shape: ``{timestamp, session_id, agent, tool, status,
    details?}`` with ISO-format timestamp.
  * ``details`` is OMITTED when None (cleaner log entries).
  * Append-only — multiple log() calls accumulate, never
    overwrite.
  * OSError is SWALLOWED (the source comment says
    "Don't let logging failures break the session"). Load-bearing
    because a full disk or readonly file shouldn't kill the
    agent's tool execution.
  * log_blocked uses uppercase "BLOCKED" status and puts the
    reason inside ``details.reason``.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from ember_code.core.utils.audit import AuditLogger


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
        logger.log(
            session_id="s1",
            agent_name="editor",
            tool_name="edit_file",
            status="success",
        )
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
        logger.log("s1", "a", "t")
        entry = json.loads((tmp_path / "audit.log").read_text().splitlines()[0])
        ts = entry["timestamp"]
        # ISO-8601 starts with YYYY-MM-DDTHH:MM:SS — strict
        # enough to fail on epoch-ms or unix-seconds.
        assert ts[4] == "-" and ts[7] == "-" and ts[10] == "T"
        # And includes timezone (the ``+00:00`` suffix).
        assert "+" in ts or ts.endswith("Z")

    def test_details_omitted_when_None(self, tmp_path):
        # Tool calls without extra context (e.g. plain
        # ``get_status``) should write a clean entry with no
        # ``details`` key at all.
        logger = _make_logger(tmp_path)
        logger.log("s1", "a", "t", status="success", details=None)
        entry = json.loads((tmp_path / "audit.log").read_text().splitlines()[0])
        assert "details" not in entry

    def test_details_included_as_nested_dict(self, tmp_path):
        # When details are set, they land as a nested object —
        # NOT flattened onto the top-level entry (would
        # collide with the canonical keys like ``status``).
        logger = _make_logger(tmp_path)
        logger.log(
            "s1",
            "a",
            "t",
            details={"path": "src/x.py", "command": "edit"},
        )
        entry = json.loads((tmp_path / "audit.log").read_text().splitlines()[0])
        assert entry["details"] == {"path": "src/x.py", "command": "edit"}

    def test_default_status_is_success(self, tmp_path):
        # The default keyword arg lets call sites omit status
        # for the happy path; pin so a future change doesn't
        # silently shift the default to e.g. "unknown".
        logger = _make_logger(tmp_path)
        logger.log("s1", "a", "t")
        entry = json.loads((tmp_path / "audit.log").read_text().splitlines()[0])
        assert entry["status"] == "success"


class TestAppendBehaviour:
    def test_multiple_logs_accumulate(self, tmp_path):
        # The whole point of an audit log is the append history.
        # Calling log() N times must produce N lines, never
        # overwrite. Pin via the file's line count.
        logger = _make_logger(tmp_path)
        for i in range(5):
            logger.log(f"s{i}", "agent", f"tool{i}")
        lines = (tmp_path / "audit.log").read_text().splitlines()
        assert len([ln for ln in lines if ln]) == 5

    def test_preserves_existing_content_on_construct(self, tmp_path):
        # Constructing a new logger over an existing file must
        # NOT truncate. The constructor's mkdir() is the only
        # filesystem side-effect — open is "a" (append).
        path = tmp_path / "audit.log"
        path.write_text('{"pre-existing": true}\n')
        logger = _make_logger(tmp_path)
        logger.log("s1", "a", "t")
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
    in-flight tool call."""

    def test_os_error_does_not_propagate(self, tmp_path):
        # The source has an explicit try/except OSError that
        # swallows the failure. Pin it — a refactor that
        # narrows or removes the except would silently start
        # crashing sessions on disk-full scenarios.
        logger = _make_logger(tmp_path)
        # Patch open() to raise OSError unconditionally.
        with patch("ember_code.core.utils.audit.open", side_effect=OSError("disk full")):
            # Must NOT raise.
            logger.log("s1", "a", "t")

    def test_enabled_false_silently_skips(self, tmp_path):
        # ``_enabled = False`` is the kill-switch (no public
        # API today, but the field exists for tests / future
        # config). Pin that toggling it off is a clean no-op
        # rather than a half-write.
        logger = _make_logger(tmp_path)
        logger._enabled = False
        logger.log("s1", "a", "t")
        # File may not even exist (no write happened).
        path = tmp_path / "audit.log"
        if path.exists():
            assert path.read_text() == ""


class TestLogBlocked:
    """``log_blocked`` is the dedicated permission-deny logger.
    Distinct status ("BLOCKED" uppercase) + reason nested under
    details."""

    def test_uses_uppercase_BLOCKED_status(self, tmp_path):
        # The status field is the searchable signal for "find
        # all blocked tool calls". Drift to lowercase
        # "blocked" would silently miss them in audit
        # post-processing.
        logger = _make_logger(tmp_path)
        logger.log_blocked("s1", "main", "run_shell", reason="rm -rf /")
        entry = json.loads((tmp_path / "audit.log").read_text().splitlines()[0])
        assert entry["status"] == "BLOCKED"

    def test_reason_lands_in_details_dict(self, tmp_path):
        # ``reason`` is the WHY — must be greppable in the
        # audit log. Pinned in ``details.reason`` (not at top
        # level — top level reserved for canonical keys).
        logger = _make_logger(tmp_path)
        logger.log_blocked("s1", "main", "run_shell", reason="rm -rf /")
        entry = json.loads((tmp_path / "audit.log").read_text().splitlines()[0])
        assert entry["details"] == {"reason": "rm -rf /"}

    def test_log_blocked_carries_session_agent_tool(self, tmp_path):
        # The canonical fields all land — log_blocked is a
        # thin wrapper around log(), not a totally different
        # entry shape.
        logger = _make_logger(tmp_path)
        logger.log_blocked("session-x", "planner", "edit_file", reason="policy")
        entry = json.loads((tmp_path / "audit.log").read_text().splitlines()[0])
        assert entry["session_id"] == "session-x"
        assert entry["agent"] == "planner"
        assert entry["tool"] == "edit_file"

"""Tests for utils/audit.py — audit logging."""

import json

from ember_code.core.config.settings import Settings
from ember_code.core.utils.audit import AuditEntry, AuditLogger


class TestAuditLogger:
    def test_logs_to_file(self, tmp_path):
        log_path = tmp_path / "audit.log"
        settings = Settings()
        settings.storage.audit_log = str(log_path)

        logger = AuditLogger(settings)
        logger.log(
            AuditEntry.success(session_id="sess1", agent_name="editor", tool_name="edit_file")
        )

        assert log_path.exists()
        line = json.loads(log_path.read_text().strip())
        assert line["session_id"] == "sess1"
        assert line["agent"] == "editor"
        assert line["tool"] == "edit_file"
        assert line["status"] == "success"
        assert "timestamp" in line

    def test_logs_with_details(self, tmp_path):
        log_path = tmp_path / "audit.log"
        settings = Settings()
        settings.storage.audit_log = str(log_path)

        logger = AuditLogger(settings)
        logger.log(
            AuditEntry.tool_call(
                session_id="sess2",
                agent_name="bash",
                tool_name="run_shell_command",
                args="git status",
            )
        )

        line = json.loads(log_path.read_text().strip())
        assert line["details"]["args"] == "git status"

    def test_log_blocked(self, tmp_path):
        log_path = tmp_path / "audit.log"
        settings = Settings()
        settings.storage.audit_log = str(log_path)

        logger = AuditLogger(settings)
        logger.log(
            AuditEntry.blocked(
                session_id="sess3",
                agent_name="editor",
                tool_name="save_file",
                reason="Protected path: .env",
            )
        )

        line = json.loads(log_path.read_text().strip())
        # Status is now lowercase-normalised via AuditStatus.BLOCKED —
        # the pre-refactor "BLOCKED" uppercase was inconsistent with
        # the other statuses ("success" / "error") and is fixed here.
        assert line["status"] == "blocked"
        assert "Protected path" in line["details"]["reason"]

    def test_appends_multiple_entries(self, tmp_path):
        log_path = tmp_path / "audit.log"
        settings = Settings()
        settings.storage.audit_log = str(log_path)

        logger = AuditLogger(settings)
        logger.log(AuditEntry.success(session_id="s1", agent_name="a", tool_name="t1"))
        logger.log(AuditEntry.success(session_id="s1", agent_name="a", tool_name="t2"))

        lines = log_path.read_text().strip().split("\n")
        assert len(lines) == 2

    def test_creates_parent_directories(self, tmp_path):
        log_path = tmp_path / "deep" / "nested" / "audit.log"
        settings = Settings()
        settings.storage.audit_log = str(log_path)

        logger = AuditLogger(settings)
        logger.log(AuditEntry.success(session_id="s1", agent_name="a", tool_name="t"))
        assert log_path.exists()

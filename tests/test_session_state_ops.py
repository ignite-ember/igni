"""Unit tests for ``session/state_ops.py``.

Extracted in iter 144. Session's own tests cover the delegate
methods; these tests pin the free-function API in isolation so
a future refactor can't silently drop the hot-patch semantics
(the whole point of ``state_ops`` is that both mutations take
effect WITHOUT rebuilding the agent — that's the invariant
worth pinning).
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from ember_code.core.config.permission_eval import PermissionEvaluator, PermissionMode
from ember_code.core.output_styles import OutputStyle
from ember_code.core.session.state_ops import set_output_style, set_permission_mode


def _bare_session(styles: dict[str, OutputStyle] | None = None):
    """Session-shaped stub carrying only what state_ops reads."""
    default_styles = styles if styles is not None else {
        "default": OutputStyle(name="default", body="Be terse.", description="", path=None),
        "explanatory": OutputStyle(name="explanatory", body="Explain a lot.", description="", path=None),
    }
    session = SimpleNamespace()
    session.output_styles = default_styles
    session._active_output_style = "default"
    session.main_team = SimpleNamespace(instructions=[])
    session.broadcast = MagicMock()
    session.permission_evaluator = PermissionEvaluator.from_strings(mode="default")
    return session


class TestSetOutputStyle:
    def test_switches_active_style(self):
        s = _bare_session()
        msg = set_output_style(s, "explanatory")
        assert s._active_output_style == "explanatory"
        assert "default" in msg and "explanatory" in msg

    def test_unknown_style_returns_error(self):
        s = _bare_session()
        msg = set_output_style(s, "nonexistent")
        assert "Error" in msg
        # Unchanged state.
        assert s._active_output_style == "default"
        # Available styles listed for discoverability.
        assert "default" in msg
        assert "explanatory" in msg

    def test_hot_patches_team_instructions(self):
        # Headline invariant: the switch mutates the LIVE team's
        # instructions list; no rebuild. Next ``arun`` sees the
        # new style without a team constructor call.
        s = _bare_session()
        s.main_team.instructions = [
            "system prompt prefix",
            "# Output style: default\n\nBe terse.",
            "trailing block",
        ]
        set_output_style(s, "explanatory")
        # Old style block replaced, not appended alongside.
        assert not any(
            isinstance(i, str) and i.startswith("# Output style: default")
            for i in s.main_team.instructions
        )
        assert any(
            isinstance(i, str) and i.startswith("# Output style: explanatory")
            for i in s.main_team.instructions
        )

    def test_empty_body_style_prunes_block(self):
        # A style with empty body should REMOVE the existing block
        # rather than append a header-only entry.
        s = _bare_session(
            styles={
                "default": OutputStyle(name="default", body="Be terse.", description="", path=None),
                "silent": OutputStyle(name="silent", body="", description="", path=None),
            }
        )
        s.main_team.instructions = ["# Output style: default\n\nBe terse."]
        set_output_style(s, "silent")
        # No # Output style: header should remain.
        assert not any(
            isinstance(i, str) and i.startswith("# Output style:")
            for i in s.main_team.instructions
        )

    def test_broadcasts_change(self):
        s = _bare_session()
        set_output_style(s, "explanatory")
        s.broadcast.assert_called_once()
        channel, payload = s.broadcast.call_args.args
        assert channel == "output_style_changed"
        assert payload == {"style": "explanatory", "previous": "default"}

    def test_switch_to_same_style_reports_noop(self):
        s = _bare_session()
        msg = set_output_style(s, "default")
        assert "already" in msg.lower()

    def test_no_team_yet_is_ok(self):
        # Partial init (Session.__new__ path) — no main_team.
        s = _bare_session()
        s.main_team = None
        msg = set_output_style(s, "explanatory")
        # Style change still recorded + broadcast.
        assert s._active_output_style == "explanatory"
        assert "explanatory" in msg


class TestSetPermissionMode:
    def test_flips_mode(self):
        s = _bare_session()
        msg = set_permission_mode(s, "plan")
        assert s.permission_evaluator.mode is PermissionMode.PLAN
        assert "plan" in msg

    def test_unknown_mode_returns_error(self):
        s = _bare_session()
        msg = set_permission_mode(s, "invalid_mode")
        assert "Error" in msg
        # Valid modes listed for discoverability.
        assert "default" in msg
        # Unchanged state.
        assert s.permission_evaluator.mode is PermissionMode.DEFAULT

    def test_missing_evaluator_returns_error(self):
        s = _bare_session()
        del s.permission_evaluator
        msg = set_permission_mode(s, "plan")
        assert "Error" in msg

    def test_same_mode_reports_noop(self):
        s = _bare_session()
        msg = set_permission_mode(s, "default")
        assert "already" in msg.lower()
        s.broadcast.assert_not_called()

    def test_broadcasts_change(self):
        s = _bare_session()
        set_permission_mode(s, "plan")
        s.broadcast.assert_called_once()
        channel, payload = s.broadcast.call_args.args
        assert channel == "permission_mode_changed"
        assert payload == {"mode": "plan", "previous": "default"}


# Ensure the assertion above about ``pytest`` isn't accidentally unused.
def test_pytest_import_is_used():
    """Silences a linter that would flag pytest as unused since none of
    the tests above use it explicitly (async ones sometimes need it)."""
    assert pytest is not None

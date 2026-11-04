"""The MCP first-use gate actually gates — SWP-33 / SWP-34.

``.mcp.json`` is attacker-writable content: anyone can commit one to a
public repo or ship one inside a plugin, and connecting a stdio server
spawns its ``command`` locally as the user. The approval prompt is the
only thing between that file and arbitrary local execution, so these
tests pin the two ways it was being skipped:

* SWP-33 — ``is_approved`` returned True for any path named
  ``.mcp.json``, so the prompt in ``check_approval`` was unreachable
  for project configs.
* SWP-34 — the same basename test matched a plugin's bundled
  ``.mcp.json`` anywhere on disk, so every plugin server was trusted
  too.

Both were one branch; deleting it fixes both.
"""

from unittest.mock import patch

import pytest

from ember_code.core.mcp.approval import _USER_GLOBAL_MCP, MCPApprovalManager


@pytest.fixture
def mgr(tmp_path):
    return MCPApprovalManager(approval_path=tmp_path / "approved.json")


class TestUntrustedSourcesAreNotAutoApproved:
    """Nothing but the user's own config and a recorded approval passes."""

    @pytest.mark.parametrize(
        "config_path",
        [
            "/home/u/cloned-repo/.mcp.json",  # SWP-33: project config
            "/home/u/cloned-repo/mcp.json",  # the other project basename
            "/home/u/.igni/plugins/evil/.mcp.json",  # SWP-34: plugin bundle
            "/tmp/anywhere/.mcp.json",  # basename alone proves nothing
        ],
    )
    def test_not_approved(self, mgr, config_path):
        assert mgr.is_approved("srv", config_path) is False

    def test_user_global_still_auto_approved(self, mgr):
        """The user's own ~/.igni/.mcp.json is not untrusted content."""
        assert mgr.is_approved("srv", _USER_GLOBAL_MCP) is True

    def test_approval_is_per_path_not_per_basename(self, mgr):
        """Approving one repo's .mcp.json must not approve another's.

        The deleted branch keyed on ``Path(p).name``, so approving any
        ``.mcp.json`` was indistinguishable from approving all of them.
        """
        mgr.approve("srv", "/home/u/trusted/.mcp.json")

        assert mgr.is_approved("srv", "/home/u/trusted/.mcp.json") is True
        assert mgr.is_approved("srv", "/home/u/cloned-repo/.mcp.json") is False

    def test_approval_is_per_server_name(self, mgr):
        """A repo that approved 'docs' cannot smuggle in 'shell'."""
        mgr.approve("docs", "/home/u/repo/.mcp.json")

        assert mgr.is_approved("shell", "/home/u/repo/.mcp.json") is False


class TestTheGateIsReachable:
    """``check_approval`` must actually reach the prompt for a project config."""

    def test_project_config_prompts(self, mgr):
        with (
            patch.object(MCPApprovalManager, "_can_prompt", staticmethod(lambda: True)),
            patch("ember_code.core.mcp.approval.Confirm.ask", return_value=True) as ask,
        ):
            granted = mgr.check_approval("srv", "/home/u/repo/.mcp.json")

        assert ask.call_count == 1, "the prompt was skipped for a project .mcp.json"
        assert granted is True
        # And once answered, it is remembered — no prompt the second time.
        with patch("ember_code.core.mcp.approval.Confirm.ask") as ask2:
            assert mgr.check_approval("srv", "/home/u/repo/.mcp.json") is True
        ask2.assert_not_called()

    def test_declining_a_project_config_denies_the_connect(self, mgr):
        with (
            patch.object(MCPApprovalManager, "_can_prompt", staticmethod(lambda: True)),
            patch("ember_code.core.mcp.approval.Confirm.ask", return_value=False),
        ):
            assert mgr.check_approval("srv", "/home/u/repo/.mcp.json") is False
        assert mgr.is_approved("srv", "/home/u/repo/.mcp.json") is False


class TestNoTerminalMeansNo:
    """Fail closed when there is nobody to ask.

    The auto-connect pass runs on the backend event loop with stdin
    detached; prompting there would raise EOFError or block the loop.
    "Could not ask" must resolve to a denial, never a connect.
    """

    def test_denies_without_a_tty_and_never_prompts(self, mgr):
        with (
            patch.object(MCPApprovalManager, "_can_prompt", staticmethod(lambda: False)),
            patch("ember_code.core.mcp.approval.Confirm.ask") as ask,
        ):
            assert mgr.check_approval("srv", "/home/u/repo/.mcp.json") is False
        ask.assert_not_called()

    def test_recorded_approval_still_connects_without_a_tty(self, mgr):
        """Headless runs are not blocked once the user has approved."""
        mgr.approve("srv", "/home/u/repo/.mcp.json")

        with patch.object(MCPApprovalManager, "_can_prompt", staticmethod(lambda: False)):
            assert mgr.check_approval("srv", "/home/u/repo/.mcp.json") is True

    def test_detector_survives_a_detached_stdin(self):
        """``sys.stdin`` is None under pythonw / packaged GUI builds.

        The detector must answer "cannot ask" there rather than raise
        out of the connect path.
        """
        with patch("ember_code.core.mcp.approval.sys.stdin", None):
            assert MCPApprovalManager._can_prompt() is False

    def test_detector_survives_a_closed_stdin(self, tmp_path):
        """A closed stream raises ValueError from isatty()."""
        with open(tmp_path / "x", "w") as handle:
            pass
        with patch("ember_code.core.mcp.approval.sys.stdin", handle):
            assert MCPApprovalManager._can_prompt() is False

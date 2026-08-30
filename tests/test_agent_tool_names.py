"""Telling somebody at startup, not mid-task.

An agent naming a tool igni does not have loads without complaint and
builds without complaint. It raises when something calls it — which is
whoever happened to invoke it, doing something else, possibly weeks
after the name was written.

The check runs once the pools are built. That timing is the point:
custom Python tools register when they are discovered, so asking any
earlier would call somebody's own tool a typo. Two things it must never
do — treat an MCP name as a mistake, or stop a session from starting.
"""

from __future__ import annotations

from ember_code.core.tools.registry import ToolRegistry
from ember_code.core.tools.tool_spec import ToolResolutionRequest


class TestWhatResolvesAndWhatDoesNot:
    def test_a_real_tool_resolves(self, tmp_path):
        result = ToolRegistry(base_dir=str(tmp_path)).resolve_typed(
            ToolResolutionRequest(tool_names=["Bash", "Read"])
        )

        assert result.unknown == []

    def test_a_typo_is_reported_rather_than_raised(self, tmp_path):
        """``resolve`` raises; the diagnostic path must not, or checking
        would be as disruptive as the failure it is warning about."""
        result = ToolRegistry(base_dir=str(tmp_path)).resolve_typed(
            ToolResolutionRequest(tool_names=["WebSarch", "Bash"])
        )

        assert result.unknown == ["WebSarch"]

    def test_an_mcp_name_is_not_a_typo(self, tmp_path):
        """Those connect after the registry is built, so nothing here
        can know them."""
        result = ToolRegistry(base_dir=str(tmp_path)).resolve_typed(
            ToolResolutionRequest(tool_names=["MCP:github"])
        )

        assert result.unknown == []


class TestTheSessionCheck:
    """Driven against a bare Session shell — constructing a whole one
    needs a database and a model catalogue, and neither is what this is
    about."""

    def _session(self, tmp_path, agents):
        from ember_code.core.session.core import Session

        session = Session.__new__(Session)
        session.project_dir = tmp_path
        session.pool = type("Pool", (), {"list_agents": lambda self: agents})()
        return session

    @staticmethod
    def _agent(name: str, tools: list[str]):
        return type("Defn", (), {"name": name, "tools": tools})()

    def test_a_clean_agent_reports_nothing(self, tmp_path):
        session = self._session(tmp_path, [self._agent("reviewer", ["Bash", "Read"])])

        session._check_agent_tools()

        assert session.unknown_agent_tools() == {}

    def test_a_typo_is_named_with_its_agent(self, tmp_path):
        session = self._session(tmp_path, [self._agent("reviewer", ["WebSarch", "Bash"])])

        session._check_agent_tools()

        assert session.unknown_agent_tools() == {"reviewer": ["WebSarch"]}

    def test_an_agent_with_no_tools_is_skipped(self, tmp_path):
        session = self._session(tmp_path, [self._agent("thinker", [])])

        session._check_agent_tools()

        assert session.unknown_agent_tools() == {}

    def test_it_logs_what_is_available(self, tmp_path, caplog):
        """The fix is usually visible in the list."""
        session = self._session(tmp_path, [self._agent("reviewer", ["WebSarch"])])

        with caplog.at_level("WARNING"):
            session._check_agent_tools()

        assert "WebSarch" in caplog.text
        assert "WebSearch" in caplog.text  # in the available list

    def test_a_broken_check_does_not_stop_a_session(self, tmp_path):
        """It is a diagnostic. Failing to produce it must cost the
        diagnostic, not the session."""

        class Exploding:
            def list_agents(self):
                raise RuntimeError("no pool")

        session = self._session(tmp_path, [])
        session.pool = Exploding()

        session._check_agent_tools()

        assert session.unknown_agent_tools() == {}

"""Tests for ``Session.refresh_codeindex_availability``.

The bug being guarded: the main agent's system prompt picks one of
two variants (``main_agent.codeindex.md`` vs ``main_agent.md``) at
session ``__init__`` based on whether the chroma has the current
HEAD's commit indexed. If a later ``/codeindex resync`` (or
``/codeindex sync``) flips that flag, the agent's prompt stays
stale — the user runs ``/codeindex resync``, waits, sees the
panel turn green with ``CodeIndex ✓``, asks the agent about the
repo, and the agent says *"CodeIndex isn't active for this
session."* This was reported on v0.5.10.

``refresh_codeindex_availability`` re-derives the flag from the
current chroma + manifest and rebuilds the pool + main team if it
changed. These tests pin:

  * the noop case (flag unchanged → no rebuild)
  * the flip-up case (False → True → rebuild)
  * the flip-down case (True → False → rebuild)
  * the rebuild preserves current MCP wiring

We bypass ``Session.__init__`` (heavy) and assemble just the
attributes the method reads.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from ember_code.core.config.settings import Settings
from ember_code.core.pool import AgentDefinition, AgentPool, AgentPriority
from ember_code.core.session.core import Session


def _make_session(
    *,
    available: bool,
    head_sha: str = "abc1234",
    chroma_has_commit: bool = False,
) -> Session:
    """Construct a Session-shaped object carrying just what
    ``refresh_codeindex_availability`` touches."""
    sess = Session.__new__(Session)
    sess._codeindex_available = available
    sess.settings = MagicMock()
    sess.project_dir = "/fake/project"
    sess._disabled_plugins = set()
    sess.plugin_loader = MagicMock()
    sess.mcp_manager = MagicMock()
    sess.mcp_manager.list_connected.return_value = []
    sess.mcp_manager._clients = {}
    sess.pool = MagicMock()
    sess.code_index = MagicMock()
    sess.code_index.has_commit.return_value = chroma_has_commit
    sess.code_index_sync = MagicMock()
    sess.code_index_sync.current_sha.return_value = head_sha
    # ``_build_main_agent`` is heavy (touches Agno) — replace with a
    # marker so we can detect that the rebuild happened.
    sess._build_main_agent = MagicMock(return_value="rebuilt-main-team")  # type: ignore[method-assign]
    sess.main_team = "initial-main-team"
    return sess


class TestRefreshCodeIndexAvailability:
    def test_noop_when_flag_unchanged_true(self):
        """Both before-state and current-chroma-state say "available" —
        nothing to do; expensive rebuild must NOT fire."""
        sess = _make_session(available=True, chroma_has_commit=True)

        changed = sess.refresh_codeindex_availability()

        assert changed is False
        assert sess._codeindex_available is True
        sess.pool.load_definitions.assert_not_called()
        sess.pool.build_agents.assert_not_called()
        sess._build_main_agent.assert_not_called()
        assert sess.main_team == "initial-main-team"

    def test_noop_when_flag_unchanged_false(self):
        """Symmetric: both say "not available" — still no rebuild."""
        sess = _make_session(available=False, chroma_has_commit=False)

        changed = sess.refresh_codeindex_availability()

        assert changed is False
        sess.pool.load_definitions.assert_not_called()
        sess.pool.build_agents.assert_not_called()
        sess._build_main_agent.assert_not_called()

    def test_rebuilds_when_flag_flips_up(self):
        """The reported bug: session started with empty chroma
        (``_codeindex_available=False``); resync populated it
        (``has_commit=True``); the agent must be rebuilt with the
        ``main_agent.codeindex.md`` prompt variant."""
        sess = _make_session(available=False, chroma_has_commit=True)

        changed = sess.refresh_codeindex_availability()

        assert changed is True
        assert sess._codeindex_available is True
        # Pool entries cleared first (forces re-load — load_definitions
        # is a noop without this).
        sess.pool.clear_definitions.assert_called_once()
        clear_kwargs = sess.pool.clear_definitions.call_args.kwargs
        assert clear_kwargs.get("preserve_ephemeral") is True, (
            "ephemeral agents (from /agents create) must not be wiped"
        )
        # Pool reloaded with the new flag — picks ``<name>.codeindex.md``
        # variants per specialist.
        sess.pool.load_definitions.assert_called_once()
        kwargs = sess.pool.load_definitions.call_args.kwargs
        assert kwargs.get("codeindex_available") is True
        # Plugins re-applied.
        sess.plugin_loader.apply_to_agents.assert_called_once()
        # Specialists rebuilt — preserving the (empty) MCP wiring.
        sess.pool.build_agents.assert_called_once()
        # Main team rebuilt with the new prompt.
        sess._build_main_agent.assert_called_once()
        assert sess.main_team == "rebuilt-main-team"

    def test_rebuilds_when_flag_flips_down(self):
        """Symmetric: chroma got wiped externally, ``has_commit`` is
        now False — rebuild with the plain ``main_agent.md`` variant
        so the agent stops claiming codeindex is available."""
        sess = _make_session(available=True, chroma_has_commit=False)

        changed = sess.refresh_codeindex_availability()

        assert changed is True
        assert sess._codeindex_available is False
        sess.pool.load_definitions.assert_called_once()
        assert sess.pool.load_definitions.call_args.kwargs.get("codeindex_available") is False
        sess._build_main_agent.assert_called_once()

    def test_passes_through_current_mcp_clients_on_rebuild(self):
        """A rebuild mid-session must not lose MCP tools — agents need
        the currently-connected MCP clients re-wired into them."""
        sess = _make_session(available=False, chroma_has_commit=True)
        # Pretend an MCP server is connected.
        fake_client = MagicMock(name="fake_mcp_client")
        sess.mcp_manager.list_connected.return_value = ["dev-server"]
        sess.mcp_manager._clients = {"dev-server": fake_client}

        sess.refresh_codeindex_availability()

        sess.pool.build_agents.assert_called_once()
        kwargs = sess.pool.build_agents.call_args.kwargs
        assert kwargs.get("mcp_clients") == {"dev-server": fake_client}

    def test_no_rebuild_when_head_unknown(self):
        """If there's no git HEAD (not a repo) the flag stays False
        and never flips up — no rebuild storm on every sync."""
        sess = _make_session(available=False, head_sha="", chroma_has_commit=False)

        changed = sess.refresh_codeindex_availability()

        assert changed is False
        sess._build_main_agent.assert_not_called()


class TestRefreshActuallySwitchesPromptVariants:
    """End-to-end integration test against a real ``AgentPool``.

    The unit tests above with a mocked pool only verify
    ``refresh_codeindex_availability`` *calls* ``load_definitions``
    — they can't detect whether that actually swaps the on-disk
    prompt file (plain ``.md`` vs ``.codeindex.md``).

    The real ``AgentPool._load_directory`` skips upsert when
    ``priority`` isn't strictly greater than the existing entry's
    priority. Calling ``load_definitions`` twice with the same
    sources will therefore *not* refresh the picked prompt
    variant unless the refresh first clears the prior entries.
    Without that clear, the user-reported bug ("agent still says
    CodeIndex isn't active after resync") never gets fixed even
    though the unit tests pass.
    """

    def _write_agent(self, path, name: str, body: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\nname: {name}\ndescription: {name}\n---\n{body}\n")

    def test_refresh_switches_prompt_to_codeindex_variant(self, tmp_path, monkeypatch):
        project = tmp_path / "proj"
        # Two variants of the same agent — only one is loaded at a
        # time, picked by ``codeindex_available``.
        self._write_agent(
            project / ".ember" / "agents" / "explorer.md",
            "explorer",
            "PLAIN-VARIANT: no codeindex tools available.",
        )
        self._write_agent(
            project / ".ember" / "agents" / "explorer.codeindex.md",
            "explorer",
            "CODEINDEX-VARIANT: codeindex_query and codeindex_tree are available.",
        )

        settings = Settings()

        # Stand up a Session-shaped object the refresh will operate on.
        sess = Session.__new__(Session)
        sess.settings = settings
        sess.project_dir = project
        sess._disabled_plugins = set()
        sess.plugin_loader = MagicMock()
        sess.mcp_manager = MagicMock()
        sess.mcp_manager.list_connected.return_value = []
        sess.mcp_manager._clients = {}
        sess.pool = AgentPool()
        sess.code_index = MagicMock()
        sess.code_index_sync = MagicMock()
        sess.code_index_sync.current_sha.return_value = "abc1234"
        sess._build_main_agent = MagicMock(return_value="main")  # type: ignore[method-assign]
        sess.main_team = None

        # Initial state: chroma empty → load plain variant.
        sess._codeindex_available = False
        sess.code_index.has_commit.return_value = False
        sess.pool.load_definitions(settings, project_dir=project, codeindex_available=False)
        defn = sess.pool.get_definition("explorer")
        assert "PLAIN-VARIANT" in defn.system_prompt, (
            "before refresh, the plain prompt should be loaded"
        )

        # Sync runs, chroma now has the commit.
        sess.code_index.has_commit.return_value = True

        changed = sess.refresh_codeindex_availability()
        assert changed is True

        defn = sess.pool.get_definition("explorer")
        assert "CODEINDEX-VARIANT" in defn.system_prompt, (
            "after refresh with chroma populated, the codeindex prompt "
            "variant must be loaded — otherwise the agent keeps the "
            "old prompt and the user-reported bug persists"
        )

    def test_refresh_preserves_ephemeral_agents(self, tmp_path):
        """Ephemeral agents (priority 10, created mid-session via
        ``/agents create``) must survive a refresh. The refresh
        clears base-priority entries to force prompt-variant
        re-picking, but ephemerals are higher-priority and live
        outside the ``.ember/agents/`` reload path."""
        project = tmp_path / "proj"
        self._write_agent(project / ".ember" / "agents" / "explorer.md", "explorer", "plain")

        settings = Settings()
        pool = AgentPool()
        pool.load_definitions(settings, project_dir=project, codeindex_available=False)
        # Inject an ephemeral by hand.
        ephemeral_def = AgentDefinition(
            name="custom-agent-mid-session",
            description="created by /agents create",
            system_prompt="ephemeral prompt body",
        )
        pool._definitions["custom-agent-mid-session"] = (ephemeral_def, AgentPriority.EPHEMERAL)

        sess = Session.__new__(Session)
        sess.settings = settings
        sess.project_dir = project
        sess._disabled_plugins = set()
        sess.plugin_loader = MagicMock()
        sess.mcp_manager = MagicMock()
        sess.mcp_manager.list_connected.return_value = []
        sess.mcp_manager._clients = {}
        sess.pool = pool
        sess.code_index = MagicMock()
        sess.code_index.has_commit.return_value = True
        sess.code_index_sync = MagicMock()
        sess.code_index_sync.current_sha.return_value = "abc1234"
        sess._build_main_agent = MagicMock(return_value="main")  # type: ignore[method-assign]
        sess.main_team = None
        sess._codeindex_available = False

        sess.refresh_codeindex_availability()

        assert "custom-agent-mid-session" in pool._definitions, (
            "ephemeral agent must survive a codeindex availability refresh"
        )

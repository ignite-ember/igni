"""Tests for session/core.py — Session construction and message handling."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ember_code.core.config.settings import Settings


def _session_patches(**overrides):
    """Return a list of patch objects for all Session dependencies.

    *overrides* lets callers change specific return_values, e.g.
    ``_session_patches(load_project_context="ctx")``.
    """
    defaults = {
        "initialize_project": None,
        "setup_db": None,
        "PermissionGuard": None,
        "AuditLogger": None,
        "HookLoader": None,
        "HookExecutor": None,
        "load_project_context": "",
        "AgentPool": None,
        "SkillPool": None,
        "ModelRegistry": None,
        "MCPClientManager": None,
        "SessionPersistence": None,
        "SessionMemoryManager": None,
        "SessionKnowledgeManager": None,
        "CloudCredentials": None,
        "CodeIndex": None,
        "CodeIndexSyncManager": None,
        "ToolRegistry": None,
        "ToolPermissions": None,
        "create_learning_machine": None,
        "ToolEventHook": None,
        "_create_reasoning_tools": None,
        "_create_guardrails": None,
        "CompressionManager": None,
        "Agent": None,
        "load_prompt": "You are an assistant.",
    }
    defaults.update(overrides)

    patches = []
    for name, rv in defaults.items():
        target = f"ember_code.core.session.core.{name}"
        # For classes (uppercase first letter), don't set return_value so
        # the mock acts as a callable that returns a fresh MagicMock.
        if name[0].isupper():
            p = patch(target)
            patches.append(p)
        else:
            patches.append(patch(target, return_value=rv))

    return patches


def _start_patches(patches):
    mocks = {}
    for p in patches:
        mock = p.start()
        mocks[p.attribute] = mock
    # ModelRegistry().get_context_window() must return an int for min()
    if "ModelRegistry" in mocks:
        mocks["ModelRegistry"].return_value.get_context_window.return_value = 128_000
    # CloudCredentials() defaults to a logged-out instance
    if "CloudCredentials" in mocks:
        cc = mocks["CloudCredentials"].return_value
        cc.is_authenticated = False
        cc.access_token = None
        cc.org_id = None
        cc.org_name = None
        cc.email = None
    return list(mocks.values())


def _stop_patches(patches):
    for p in patches:
        p.stop()


class TestSessionConstruction:
    """Test Session initialization without hitting Agno or the network."""

    @pytest.fixture
    def _patch_deps(self, tmp_path):
        patches = _session_patches()
        _start_patches(patches)
        yield
        _stop_patches(patches)

    def test_creates_session_with_defaults(self, tmp_path, _patch_deps):
        from ember_code.core.session.core import Session

        settings = Settings()
        session = Session(settings, project_dir=tmp_path)

        assert session.project_dir == tmp_path
        assert session.session_id is not None
        assert len(session.session_id) == 8
        assert session.settings is settings

    def test_creates_session_with_resume_id(self, tmp_path, _patch_deps):
        from ember_code.core.session.core import Session

        session = Session(Settings(), project_dir=tmp_path, resume_session_id="my-session")
        assert session.session_id == "my-session"
        assert session.session_named is True

    def test_creates_session_with_additional_dirs(self, tmp_path, _patch_deps):
        from ember_code.core.session.core import Session

        extra = tmp_path / "extra"
        extra.mkdir()
        session = Session(Settings(), project_dir=tmp_path, additional_dirs=[extra])
        assert session.workspace.is_multi
        assert extra.resolve() in session.workspace.all_dirs

    def test_cloud_connected_false_by_default(self, tmp_path, _patch_deps):
        from ember_code.core.session.core import Session

        session = Session(Settings(), project_dir=tmp_path)
        assert session.cloud_connected is False
        assert session.cloud_org_id is None
        assert session.cloud_org_name is None

    def test_cloud_connected_true_with_token(self, tmp_path):
        patches = _session_patches()
        _start_patches(patches)
        try:
            from ember_code.core.session.core import CloudCredentials as cc_patched

            cc = cc_patched.return_value
            cc.is_authenticated = True
            cc.access_token = "tok-123"
            cc.org_id = "org_42"
            cc.org_name = "Acme"

            from ember_code.core.session.core import Session

            session = Session(Settings(), project_dir=tmp_path)
            assert session.cloud_connected is True
            assert session.cloud_org_id == "org_42"
            assert session.cloud_org_name == "Acme"
        finally:
            _stop_patches(patches)


class TestSessionMessageHandling:
    @pytest.fixture
    def session(self, tmp_path):
        patches = _session_patches()
        _start_patches(patches)

        from ember_code.core.session.core import Session

        s = Session(Settings(), project_dir=tmp_path)

        # Configure mocks for message handling
        mock_hook_result = MagicMock()
        mock_hook_result.should_continue = True
        s.hook_executor.execute = AsyncMock(return_value=mock_hook_result)
        s.persistence.auto_name = AsyncMock()
        s.audit.log = MagicMock()

        # Mock the team response
        mock_response = MagicMock()
        mock_response.content = "Hello! I can help."
        mock_response.metrics = None
        s.main_team.arun = AsyncMock(return_value=mock_response)
        s.main_team.run_response = MagicMock(metrics=None)

        yield s
        _stop_patches(patches)

    @pytest.mark.asyncio
    async def test_handle_message_returns_response(self, session):
        with patch("ember_code.core.session.core.extract_response_text", return_value="Hello!"):
            result = await session.handle_message("Hi there")
            assert result == "Hello!"
            # Message includes a UTC timestamp prefix
            call_args = session.main_team.arun.call_args
            assert call_args[1]["stream"] is False
            assert "Hi there" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_handle_message_blocked_by_hook(self, session):
        mock_hook_result = MagicMock()
        mock_hook_result.should_continue = False
        mock_hook_result.message = "Blocked by policy"
        session.hook_executor.execute = AsyncMock(return_value=mock_hook_result)

        result = await session.handle_message("do something bad")
        assert "Blocked" in result
        session.main_team.arun.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_message_error(self, session):
        session.main_team.arun = AsyncMock(side_effect=RuntimeError("LLM failed"))
        result = await session.handle_message("test")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_handle_message_error_fires_stop_failure_hook(self, session):
        """``handle_message`` errors out → ``StopFailure`` hook fires
        with the error message + type so crash-reporting plugins can
        observe in-band."""
        session.main_team.arun = AsyncMock(side_effect=RuntimeError("LLM failed"))
        await session.handle_message("test")
        # Walk the recorded calls (set up in the fixture) for the
        # StopFailure event. ``UserPromptSubmit`` also fired first;
        # we only care about the failure one here.
        events_fired = [c.kwargs.get("event") for c in session.hook_executor.execute.call_args_list]
        assert "StopFailure" in events_fired
        # Payload contract: error + error_type.
        sf_call = next(
            c
            for c in session.hook_executor.execute.call_args_list
            if c.kwargs.get("event") == "StopFailure"
        )
        payload = sf_call.kwargs["payload"]
        assert "LLM failed" in payload["error"]
        assert payload["error_type"] == "RuntimeError"


class TestSessionCompaction:
    @pytest.fixture
    def session(self, tmp_path):
        patches = _session_patches()
        _start_patches(patches)

        from ember_code.core.session.core import Session

        s = Session(Settings(), project_dir=tmp_path)
        yield s
        _stop_patches(patches)

    @pytest.mark.asyncio
    async def test_no_compaction_below_threshold(self, session):
        result = await session.compact_if_needed(1000, 10000)  # 10%
        assert result is False

    @pytest.mark.asyncio
    async def test_compacts_at_80_percent(self, session):
        mock_agno_session = MagicMock()
        mock_agno_session.runs = []
        mock_agno_session.summary = None
        session.main_team.aget_session = AsyncMock(return_value=mock_agno_session)
        session.main_team.asave_session = AsyncMock()
        # PreCompact/PostCompact hooks added — stub the executor so the
        # await in compact_if_needed resolves with should_continue=True.
        mock_hook_result = MagicMock()
        mock_hook_result.should_continue = True
        session.hook_executor.execute = AsyncMock(return_value=mock_hook_result)
        result = await session.compact_if_needed(8500, 10000)  # 85%
        assert result is True
        # Runs should have been cleared
        session.main_team.asave_session.assert_called_once()
        # PreCompact AND PostCompact should have fired around the
        # actual compaction.
        events_fired = [c.kwargs.get("event") for c in session.hook_executor.execute.call_args_list]
        assert "PreCompact" in events_fired
        assert "PostCompact" in events_fired

    @pytest.mark.asyncio
    async def test_pre_compact_hook_can_cancel_auto(self, session):
        """Returning ``should_continue=False`` from PreCompact must
        skip the actual compaction (auto path)."""
        mock_agno_session = MagicMock()
        mock_agno_session.runs = []
        session.main_team.aget_session = AsyncMock(return_value=mock_agno_session)
        session.main_team.asave_session = AsyncMock()
        veto = MagicMock()
        veto.should_continue = False
        veto.message = "blocked"
        session.hook_executor.execute = AsyncMock(return_value=veto)
        result = await session.compact_if_needed(8500, 10000)
        assert result is False
        # _compact never ran → no save call
        session.main_team.asave_session.assert_not_called()


class TestSessionLearning:
    """Test that learning is wired into Session correctly."""

    def test_learning_none_when_disabled(self, tmp_path):
        patches = _session_patches()
        _start_patches(patches)
        try:
            from ember_code.core.session.core import Session

            settings = Settings()
            settings.learning.enabled = False
            session = Session(settings, project_dir=tmp_path)
            assert session._learning is None
        finally:
            _stop_patches(patches)

    def test_learning_created_when_enabled(self, tmp_path):
        fake_lm = MagicMock()
        patches = _session_patches(create_learning_machine=fake_lm)
        _start_patches(patches)
        try:
            from ember_code.core.session.core import Session

            settings = Settings()
            settings.learning.enabled = True
            session = Session(settings, project_dir=tmp_path)
            assert session._learning is fake_lm
        finally:
            _stop_patches(patches)

    def test_learning_passed_to_team(self, tmp_path):
        """When learning is enabled the LM instance flows through to ``Agent(learning=...)``.

        Agno surfaces ``update_user_memory`` as a tool when ``learning``
        is set; ``add_learnings_to_context`` keeps the agent fed with
        prior memories. Both stay on so the agent can reach for memory
        without an extra plumbing round-trip.
        """
        fake_lm = MagicMock()
        patches = _session_patches(create_learning_machine=fake_lm)
        mocks = {}
        for p in patches:
            mock = p.start()
            mocks[p.attribute] = mock
        if "ModelRegistry" in mocks:
            mocks["ModelRegistry"].return_value.get_context_window.return_value = 128_000
        try:
            from ember_code.core.session.core import Session

            settings = Settings()
            settings.learning.enabled = True
            session = Session(settings, project_dir=tmp_path)

            agent_cls = mocks["Agent"]
            assert agent_cls.called
            call_kwargs = agent_cls.call_args[1]
            assert call_kwargs["learning"] is session._learning
            assert call_kwargs["add_learnings_to_context"] is True
        finally:
            _stop_patches(patches)

    def test_learning_not_passed_when_disabled(self, tmp_path):
        """With learning disabled, ``self._learning`` stays ``None`` and the
        Agent receives ``learning=None`` — no ``update_user_memory`` tool."""
        patches = _session_patches()
        mocks = {}
        for p in patches:
            mock = p.start()
            mocks[p.attribute] = mock
        if "ModelRegistry" in mocks:
            mocks["ModelRegistry"].return_value.get_context_window.return_value = 128_000
        try:
            from ember_code.core.session.core import Session

            settings = Settings()
            Session(settings, project_dir=tmp_path)

            agent_cls = mocks["Agent"]
            call_kwargs = agent_cls.call_args[1]
            assert call_kwargs["learning"] is None
        finally:
            _stop_patches(patches)


def _patches_with_real_context_loader():
    """Same set of mocks as ``_session_patches`` but leaves
    ``load_project_context`` un-mocked so the real loader fires and we can
    verify that rule files on disk actually reach the agent."""
    overrides = {
        "initialize_project": None,
        "setup_db": None,
        "PermissionGuard": None,
        "AuditLogger": None,
        "HookLoader": None,
        "HookExecutor": None,
        "AgentPool": None,
        "SkillPool": None,
        "ModelRegistry": None,
        "MCPClientManager": None,
        "SessionPersistence": None,
        "SessionMemoryManager": None,
        "SessionKnowledgeManager": None,
        "CloudCredentials": None,
        "CodeIndex": None,
        "CodeIndexSyncManager": None,
        "ToolRegistry": None,
        "ToolPermissions": None,
        "create_learning_machine": None,
        "ToolEventHook": None,
        "_create_reasoning_tools": None,
        "_create_guardrails": None,
        "CompressionManager": None,
        "Agent": None,
        "load_prompt": "You are an assistant.",
    }
    patches = []
    for name, rv in overrides.items():
        target = f"ember_code.core.session.core.{name}"
        if name[0].isupper():
            patches.append(patch(target))
        else:
            patches.append(patch(target, return_value=rv))
    return patches


def _agent_instructions(agent_mock) -> list[str]:
    """Pull the ``instructions`` arg out of the most recent Agent(...) call."""
    assert agent_mock.called
    kwargs = agent_mock.call_args.kwargs
    if "instructions" in kwargs:
        return list(kwargs["instructions"])
    # Fall back to positional — current code uses kwargs, but guard anyway.
    args = agent_mock.call_args.args
    for a in args:
        if isinstance(a, list):
            return list(a)
    return []


class TestRulesReachAgent:
    """End-to-end: rules loaded from disk must land in the Agent's
    ``instructions=`` argument. Without these tests the new loader paths
    (~/.ember/rules/, ~/.claude/rules/) would happily run while never
    actually influencing the AI — exactly the dead-code risk worth
    guarding against.
    """

    def _isolate_user_rules(self, monkeypatch, tmp_path, *, claude_dir=None):
        """Redirect user-rule lookups into ``tmp_path`` so the test host's
        real ``~/.ember/`` and ``~/.claude/`` never leak in."""
        from ember_code.core.utils import context as ctx_mod

        monkeypatch.setattr(ctx_mod, "USER_RULES_PATH", tmp_path / "_no_legacy.md")
        monkeypatch.setattr(ctx_mod, "USER_RULES_DIR", tmp_path / "_no_user_dir")
        monkeypatch.setattr(
            ctx_mod,
            "CLAUDE_USER_RULES_DIR",
            claude_dir if claude_dir is not None else tmp_path / "_no_claude_dir",
        )

    def _build_session(self, project_dir, settings=None):
        from ember_code.core.session.core import Session

        return Session(settings or Settings(), project_dir=project_dir)

    def test_project_ember_md_reaches_agent_instructions(self, tmp_path, monkeypatch):
        project = tmp_path / "proj"
        project.mkdir()
        (project / "ember.md").write_text("SENTINEL_PROJECT_RULE_XYZ")
        self._isolate_user_rules(monkeypatch, tmp_path)

        patches = _patches_with_real_context_loader()
        mocks = {}
        for p in patches:
            mocks[p.attribute] = p.start()
        mocks["ModelRegistry"].return_value.get_context_window.return_value = 128_000
        try:
            session = self._build_session(project)
            # 1. The loader fired and populated project_instructions.
            assert "SENTINEL_PROJECT_RULE_XYZ" in session.project_instructions
            # 2. Those instructions flowed into the Agent constructor.
            instructions = _agent_instructions(mocks["Agent"])
            assert any("SENTINEL_PROJECT_RULE_XYZ" in s for s in instructions)
        finally:
            _stop_patches(patches)

    def test_user_legacy_rules_md_reaches_agent_instructions(self, tmp_path, monkeypatch):
        from ember_code.core.utils import context as ctx_mod

        legacy = tmp_path / "rules.md"
        legacy.write_text("SENTINEL_USER_LEGACY_ABC")
        monkeypatch.setattr(ctx_mod, "USER_RULES_PATH", legacy)
        monkeypatch.setattr(ctx_mod, "USER_RULES_DIR", tmp_path / "_no_user_dir")
        monkeypatch.setattr(ctx_mod, "CLAUDE_USER_RULES_DIR", tmp_path / "_no_claude_dir")

        project = tmp_path / "proj"
        project.mkdir()

        patches = _patches_with_real_context_loader()
        mocks = {}
        for p in patches:
            mocks[p.attribute] = p.start()
        mocks["ModelRegistry"].return_value.get_context_window.return_value = 128_000
        try:
            session = self._build_session(project)
            assert "SENTINEL_USER_LEGACY_ABC" in session.project_instructions
            instructions = _agent_instructions(mocks["Agent"])
            assert any("SENTINEL_USER_LEGACY_ABC" in s for s in instructions)
        finally:
            _stop_patches(patches)

    def test_user_rules_directory_reaches_agent_instructions(self, tmp_path, monkeypatch):
        """``~/.ember/rules/*.md`` (the new directory source) must reach the agent."""
        from ember_code.core.utils import context as ctx_mod

        user_dir = tmp_path / "ember-rules"
        user_dir.mkdir()
        (user_dir / "commit-style.md").write_text("SENTINEL_USER_DIR_RULE_111")
        (user_dir / "lint.md").write_text("SENTINEL_USER_DIR_RULE_222")

        monkeypatch.setattr(ctx_mod, "USER_RULES_PATH", tmp_path / "_no_legacy.md")
        monkeypatch.setattr(ctx_mod, "USER_RULES_DIR", user_dir)
        monkeypatch.setattr(ctx_mod, "CLAUDE_USER_RULES_DIR", tmp_path / "_no_claude_dir")

        project = tmp_path / "proj"
        project.mkdir()

        patches = _patches_with_real_context_loader()
        mocks = {}
        for p in patches:
            mocks[p.attribute] = p.start()
        mocks["ModelRegistry"].return_value.get_context_window.return_value = 128_000
        try:
            session = self._build_session(project)
            assert "SENTINEL_USER_DIR_RULE_111" in session.project_instructions
            assert "SENTINEL_USER_DIR_RULE_222" in session.project_instructions
            instructions = _agent_instructions(mocks["Agent"])
            joined = "\n".join(s for s in instructions if isinstance(s, str))
            assert "SENTINEL_USER_DIR_RULE_111" in joined
            assert "SENTINEL_USER_DIR_RULE_222" in joined
        finally:
            _stop_patches(patches)

    def test_claude_rules_directory_reaches_agent_when_cross_tool_enabled(
        self, tmp_path, monkeypatch
    ):
        """``~/.claude/rules/*.md`` (the cross-tool source) must reach the agent
        when ``rules.cross_tool_support`` is enabled (default)."""
        claude_dir = tmp_path / "claude-rules"
        claude_dir.mkdir()
        (claude_dir / "git.md").write_text("SENTINEL_CLAUDE_DIR_RULE_AAA")
        self._isolate_user_rules(monkeypatch, tmp_path, claude_dir=claude_dir)

        project = tmp_path / "proj"
        project.mkdir()

        patches = _patches_with_real_context_loader()
        mocks = {}
        for p in patches:
            mocks[p.attribute] = p.start()
        mocks["ModelRegistry"].return_value.get_context_window.return_value = 128_000
        try:
            session = self._build_session(project)
            assert "SENTINEL_CLAUDE_DIR_RULE_AAA" in session.project_instructions
            instructions = _agent_instructions(mocks["Agent"])
            assert any("SENTINEL_CLAUDE_DIR_RULE_AAA" in s for s in instructions)
        finally:
            _stop_patches(patches)

    def test_claude_rules_directory_excluded_when_cross_tool_disabled(self, tmp_path, monkeypatch):
        """Same Claude rules, but with cross-tool support off: they must NOT
        appear in the agent's instructions."""
        claude_dir = tmp_path / "claude-rules"
        claude_dir.mkdir()
        (claude_dir / "git.md").write_text("SENTINEL_CLAUDE_DIR_RULE_BBB")
        self._isolate_user_rules(monkeypatch, tmp_path, claude_dir=claude_dir)

        project = tmp_path / "proj"
        project.mkdir()

        settings = Settings()
        settings.rules.cross_tool_support = False

        patches = _patches_with_real_context_loader()
        mocks = {}
        for p in patches:
            mocks[p.attribute] = p.start()
        mocks["ModelRegistry"].return_value.get_context_window.return_value = 128_000
        try:
            session = self._build_session(project, settings=settings)
            assert "SENTINEL_CLAUDE_DIR_RULE_BBB" not in session.project_instructions
            instructions = _agent_instructions(mocks["Agent"])
            assert not any("SENTINEL_CLAUDE_DIR_RULE_BBB" in s for s in instructions)
        finally:
            _stop_patches(patches)

    def test_all_three_user_sources_merge_into_agent_instructions(self, tmp_path, monkeypatch):
        """Sanity: legacy file + ember rules dir + claude rules dir all merge
        and reach the agent in a single session."""
        from ember_code.core.utils import context as ctx_mod

        legacy = tmp_path / "rules.md"
        legacy.write_text("SENTINEL_TRIPLE_LEGACY")
        ember_dir = tmp_path / "ember-rules"
        ember_dir.mkdir()
        (ember_dir / "one.md").write_text("SENTINEL_TRIPLE_EMBER")
        claude_dir = tmp_path / "claude-rules"
        claude_dir.mkdir()
        (claude_dir / "two.md").write_text("SENTINEL_TRIPLE_CLAUDE")

        monkeypatch.setattr(ctx_mod, "USER_RULES_PATH", legacy)
        monkeypatch.setattr(ctx_mod, "USER_RULES_DIR", ember_dir)
        monkeypatch.setattr(ctx_mod, "CLAUDE_USER_RULES_DIR", claude_dir)

        project = tmp_path / "proj"
        project.mkdir()

        patches = _patches_with_real_context_loader()
        mocks = {}
        for p in patches:
            mocks[p.attribute] = p.start()
        mocks["ModelRegistry"].return_value.get_context_window.return_value = 128_000
        try:
            self._build_session(project)
            instructions = _agent_instructions(mocks["Agent"])
            joined = "\n".join(s for s in instructions if isinstance(s, str))
            assert "SENTINEL_TRIPLE_LEGACY" in joined
            assert "SENTINEL_TRIPLE_EMBER" in joined
            assert "SENTINEL_TRIPLE_CLAUDE" in joined
        finally:
            _stop_patches(patches)


# ── Session._queue_rewake (asyncRewake callback target) ───────
#
# Background hooks that exit-2 fire ``_queue_rewake(text)`` → the
# text buffers in ``_pending_reminders`` until the next
# ``handle_message`` turn drains it (we can't interrupt an
# in-flight response). The executor side is tested in
# ``test_hook_async_rewake.py``; these lock down the Session-side
# glue so a refactor of the rewake path can't silently regress.


class TestSessionQueueRewake:
    """Direct unit tests for ``Session._queue_rewake``."""

    def _bare_session(self):
        from ember_code.core.session.core import Session

        session = Session.__new__(Session)
        session._pending_reminders = []
        return session

    def test_empty_text_is_dropped(self):
        # Defensive guard — a hook that exits 2 with no
        # stdout/stderr would otherwise push an empty reminder
        # that prepends ``\n\n`` to the next turn and confuses
        # the agent. Empty → no-op.
        session = self._bare_session()
        session._queue_rewake("")
        assert session._pending_reminders == []

    def test_appends_single_reminder(self):
        session = self._bare_session()
        session._queue_rewake("the hook reported")
        assert session._pending_reminders == ["the hook reported"]

    def test_accumulates_in_call_order(self):
        # Two background hooks landing in the same gap between
        # turns must both surface to the agent, in the order
        # they fired. asyncio's single-threaded scheduling
        # makes ``list.append`` safe here — this test documents
        # the contract so an accidental swap to a set would
        # trip it.
        session = self._bare_session()
        session._queue_rewake("first")
        session._queue_rewake("second")
        session._queue_rewake("third")
        assert session._pending_reminders == ["first", "second", "third"]


# ── Session._mcp_resolver (mcp_tool hook target) ──────────────
#
# Resolver passed to ``HookExecutor`` so ``mcp_tool``-type hooks
# can invoke MCP tools without the executor knowing about the
# manager. Resolved at fire-time (not at construction) because
# ``__init__`` builds the executor BEFORE ``mcp_manager`` is
# wired. Returns ``None`` for every missing link so a broken MCP
# setup degrades gracefully rather than crashing hook execution.


class TestSessionMcpResolver:
    """``Session._mcp_resolver(server, tool)`` walks
    ``self.mcp_manager._clients[server].functions[tool]`` and
    returns ``None`` at the first missing hop."""

    def _bare_session(self):
        from ember_code.core.session.core import Session

        return Session.__new__(Session)

    def test_returns_none_when_manager_absent(self):
        # The manager attribute may be unset entirely (MCP
        # compiled out, or session built before MCP init
        # completes). ``getattr`` with default None makes the
        # chain safe.
        session = self._bare_session()
        assert session._mcp_resolver("slack", "send") is None

    def test_returns_none_when_manager_is_none(self):
        session = self._bare_session()
        session.mcp_manager = None
        assert session._mcp_resolver("slack", "send") is None

    def test_returns_none_when_server_not_in_clients(self):
        # Manager present + has _clients, but the named server
        # isn't connected. ``None`` keeps hook execution from
        # crashing on a misconfigured settings.json.
        session = self._bare_session()
        manager = MagicMock()
        manager._clients = {}
        session.mcp_manager = manager
        assert session._mcp_resolver("missing", "any") is None

    def test_returns_none_when_tool_not_in_functions(self):
        # Server connected, but doesn't expose the named tool
        # (typo in settings.json, or tool removed upstream).
        session = self._bare_session()
        client = MagicMock()
        client.functions = {"other_tool": object()}
        manager = MagicMock()
        manager._clients = {"slack": client}
        session.mcp_manager = manager
        assert session._mcp_resolver("slack", "send_message") is None

    def test_returns_function_when_fully_resolved(self):
        # Happy path — function found, returned for the executor
        # to invoke. Identity equality matters: the executor
        # awaits the SAME callable, no proxy/wrap.
        session = self._bare_session()
        target = MagicMock(name="send_message_fn")
        client = MagicMock()
        client.functions = {"send_message": target}
        manager = MagicMock()
        manager._clients = {"slack": client}
        session.mcp_manager = manager
        assert session._mcp_resolver("slack", "send_message") is target

    def test_returns_none_when_client_has_no_functions_attr(self):
        # Older client classes didn't expose ``functions``.
        # ``getattr(..., None) or {}`` in the source covers it
        # so the chain still returns None instead of raising
        # AttributeError into the hook executor.
        session = self._bare_session()
        client = MagicMock(spec=[])  # spec=[] strips functions
        manager = MagicMock()
        manager._clients = {"slack": client}
        session.mcp_manager = manager
        assert session._mcp_resolver("slack", "send") is None


# ── Session.broadcast + register_broadcast_callback ──────────
#
# Indirect coverage exists via output_styles and plan_mode tests
# (they register a capture callback to inspect the channel
# stream). These tests pin the contract itself: idempotent
# re-register, exception isolation between callbacks, defensive
# fallback when ``_broadcast_callbacks`` was never initialised,
# call order + payload identity.


class TestSessionBroadcast:
    def _bare_session(self):
        from ember_code.core.session.core import Session

        session = Session.__new__(Session)
        session._broadcast_callbacks = []
        return session

    def test_register_appends_callback(self):
        session = self._bare_session()
        cb = lambda ch, p: None  # noqa: E731
        session.register_broadcast_callback(cb)
        assert session._broadcast_callbacks == [cb]

    def test_register_is_idempotent_on_same_callback(self):
        # The /plan and /accept slash commands both call
        # ``register_broadcast_callback`` on the same transport
        # closure during session bootstrap. Idempotency means
        # re-registering doesn't fan out the same event twice
        # — load-bearing for the "callbacks fire once per
        # broadcast" contract callers rely on.
        session = self._bare_session()
        cb = lambda ch, p: None  # noqa: E731
        session.register_broadcast_callback(cb)
        session.register_broadcast_callback(cb)
        session.register_broadcast_callback(cb)
        assert len(session._broadcast_callbacks) == 1

    def test_register_distinct_callbacks_both_kept(self):
        session = self._bare_session()
        cb1 = lambda ch, p: None  # noqa: E731
        cb2 = lambda ch, p: None  # noqa: E731
        session.register_broadcast_callback(cb1)
        session.register_broadcast_callback(cb2)
        assert session._broadcast_callbacks == [cb1, cb2]

    def test_broadcast_calls_every_registered_callback(self):
        session = self._bare_session()
        captured: list[tuple[str, dict]] = []
        session.register_broadcast_callback(lambda ch, p: captured.append((ch, p)))
        session.register_broadcast_callback(lambda ch, p: captured.append((ch, p)))
        session.broadcast("plan_submitted", {"plan": "x"})
        # Both callbacks fired with the same args.
        assert captured == [
            ("plan_submitted", {"plan": "x"}),
            ("plan_submitted", {"plan": "x"}),
        ]

    def test_broadcast_preserves_call_order(self):
        # The first-registered callback fires first. Tests that
        # depend on capture-order (e.g. output_styles asserting
        # the active style lands first) rely on this.
        session = self._bare_session()
        log: list[str] = []
        session.register_broadcast_callback(lambda ch, p: log.append("first"))
        session.register_broadcast_callback(lambda ch, p: log.append("second"))
        session.register_broadcast_callback(lambda ch, p: log.append("third"))
        session.broadcast("any", {})
        assert log == ["first", "second", "third"]

    def test_broadcast_forwards_payload_identity(self):
        # The payload reaches the callback as the SAME object —
        # no defensive copy. Callers can mutate the dict
        # before/after broadcast and the callback sees it
        # consistently. Pin this so a defensive ``copy.deepcopy``
        # refactor surfaces as a deliberate behaviour change.
        session = self._bare_session()
        seen: list[object] = []
        session.register_broadcast_callback(lambda ch, p: seen.append(p))
        payload = {"plan": "x"}
        session.broadcast("plan_submitted", payload)
        assert seen[0] is payload

    def test_callback_exception_does_not_block_others(self):
        # The contract is "best-effort fan-out" — one buggy
        # subscriber must not silence the rest. Pin so a
        # refactor of the loop doesn't accidentally introduce
        # short-circuiting on first exception.
        session = self._bare_session()
        log: list[str] = []

        def boom(ch, p):
            raise RuntimeError("subscriber crashed")

        session.register_broadcast_callback(boom)
        session.register_broadcast_callback(lambda ch, p: log.append("after-boom"))
        # Must NOT raise.
        session.broadcast("channel", {})
        assert log == ["after-boom"]

    def test_broadcast_with_no_callbacks_is_noop(self):
        # Common case during session bootstrap before any
        # transport has subscribed. Don't crash.
        session = self._bare_session()
        session.broadcast("any", {})  # no raise

    def test_broadcast_without_callbacks_attribute_is_noop(self):
        # Tests routinely build ``Session.__new__`` without
        # running ``__init__`` — ``_broadcast_callbacks``
        # doesn't exist yet. The defensive ``getattr`` in the
        # source makes this a noop instead of an AttributeError.
        # Load-bearing for the test suite's session-construct
        # patterns.
        from ember_code.core.session.core import Session

        session = Session.__new__(Session)  # NO ``_broadcast_callbacks`` set
        session.broadcast("any", {})  # no raise

    def test_callbacks_can_register_during_broadcast_safely(self):
        # Edge case — a callback that registers a NEW callback
        # during the broadcast. The source iterates ``list(...)``
        # which copies the snapshot, so new callbacks don't fire
        # mid-broadcast (they wait for the next one). Pin this:
        # absent the copy, mutating the list during iteration
        # raises RuntimeError.
        session = self._bare_session()
        log: list[str] = []

        def first(ch, p):
            log.append("first")
            session.register_broadcast_callback(lambda c, p: log.append("late"))

        session.register_broadcast_callback(first)
        session.broadcast("ev", {})
        # ``first`` fires; ``late`` is registered but doesn't
        # fire this round.
        assert log == ["first"]
        # ``late`` is now in the list for the next broadcast.
        session.broadcast("ev2", {})
        assert log == ["first", "first", "late"]


class TestMainTeamToolkit:
    """Pin the shell-first design of the main team's tool catalog
    (CLAUDE_CODE_PARITY.md row 22 / commit 7e50705).

    The main team intentionally has NO ``Read`` / ``Grep`` / ``Glob``
    / ``LS`` — those overlap with ``Bash`` (``cat``, ``rg``, ``find``,
    ``ls``) and confused the model into double-search behavior. They
    stay in the registry so SUB-agents can opt in via their
    ``tools:`` frontmatter, but they're not in the main team's
    always-on set. A future refactor that re-adds them to the main
    team must be a deliberate choice — this test makes silent
    regression a failing test.
    """

    @staticmethod
    def _stub_session(
        *,
        web: str = "ask",
        fetch: str = "ask",
        codeindex_available: bool = False,
    ):
        """Cheap stub: just enough state for
        ``_resolve_main_tool_names`` to run, without booting Agno,
        the DB, etc. The method only reads ``settings.permissions``
        and ``_codeindex_available``."""
        from unittest.mock import MagicMock

        from ember_code.core.session.core import Session

        session = Session.__new__(Session)
        permissions = MagicMock()
        permissions.web_search = web
        permissions.web_fetch = fetch
        settings = MagicMock()
        settings.permissions = permissions
        session.settings = settings
        session._codeindex_available = codeindex_available
        return session

    @staticmethod
    def _stub_registry():
        """ToolRegistry stub — ``_resolve_main_tool_names`` only
        uses ``.resolve([...])`` to probe whether a tool's
        dependencies are importable. Return a no-op success so
        every optional tool gets added when its permission/flag
        gate allows it. Per-test variants override this to
        simulate ``ImportError`` for missing extras."""
        from unittest.mock import MagicMock

        return MagicMock(resolve=MagicMock(return_value=[]))

    def test_core_toolkit_is_shell_first(self):
        session = self._stub_session()
        names = session._resolve_main_tool_names(self._stub_registry())
        # The 5 always-on tools — shell-first design.
        for required in ("Write", "Edit", "Bash", "Schedule", "NotebookEdit"):
            assert required in names, f"main team must always include {required}"

    def test_main_toolkit_excludes_registry_only_tools(self):
        # Hard contract: Read/Grep/Glob/LS/Python are registry-only
        # (available to sub-agents that opt in). They MUST NOT
        # appear in the main team's toolkit. A regression here
        # silently re-introduces tool overlap with Bash.
        session = self._stub_session(codeindex_available=True)
        names = session._resolve_main_tool_names(self._stub_registry())
        for forbidden in ("Read", "Grep", "Glob", "LS", "Python"):
            assert forbidden not in names, (
                f"{forbidden} is registry-only — sub-agents opt in via "
                f"frontmatter; main team uses Bash for this. See "
                f"CLAUDE_CODE_PARITY.md row 22."
            )

    def test_web_tools_omitted_when_denied(self):
        # ``permissions.web_search: deny`` (e.g. ``--no-web`` CLI
        # flag) hides the WebSearch toolkit from the agent's
        # catalog entirely. Same for WebFetch.
        session = self._stub_session(web="deny", fetch="deny")
        names = session._resolve_main_tool_names(self._stub_registry())
        assert "WebSearch" not in names
        assert "WebFetch" not in names

    def test_web_tools_included_when_allowed(self):
        session = self._stub_session(web="ask", fetch="ask")
        names = session._resolve_main_tool_names(self._stub_registry())
        assert "WebSearch" in names
        assert "WebFetch" in names

    def test_web_tools_silently_skipped_on_import_error(self):
        # Optional extras may not be installed (e.g. headless
        # builds without DuckDuckGo). The registry raises
        # ``ImportError``; ``_resolve_main_tool_names`` swallows
        # it and just omits the tool — no crash, no warning to
        # the user that doesn't want the dep anyway.
        from unittest.mock import MagicMock

        session = self._stub_session(web="ask", fetch="ask")
        registry = MagicMock()
        registry.resolve = MagicMock(side_effect=ImportError("no ddgs"))
        names = session._resolve_main_tool_names(registry)
        assert "WebSearch" not in names
        assert "WebFetch" not in names
        # Core tools still landed.
        assert "Bash" in names

    def test_codeindex_included_only_when_available(self):
        without = self._stub_session(codeindex_available=False)
        with_ = self._stub_session(codeindex_available=True)
        names_without = without._resolve_main_tool_names(self._stub_registry())
        names_with = with_._resolve_main_tool_names(self._stub_registry())
        assert "CodeIndex" not in names_without
        assert "CodeIndex" in names_with

    def test_core_tools_constant_is_immutable(self):
        # The 5-tool core is class-level + a tuple so a future
        # contributor can't accidentally ``Session._MAIN_CORE_TOOLS
        # .append("Read")`` and silently regress the design.
        from ember_code.core.session.core import Session

        assert isinstance(Session._MAIN_CORE_TOOLS, tuple)
        assert Session._MAIN_CORE_TOOLS == (
            "Write",
            "Edit",
            "Bash",
            "Schedule",
            "NotebookEdit",
        )

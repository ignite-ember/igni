"""``SessionMessageHandler`` held three things by value that the session
changes underneath it, and one of them was never there at all.

* **The metrics.** ``handle`` asked ``team.run_response.metrics`` for the
  turn's input-token count. agno 2.6.12's ``Agent`` and ``Team`` have no
  ``run_response`` attribute — the name exists only as a keyword
  parameter — so the ``getattr`` chain returned ``None`` on every turn
  and the block behind ``if metrics:`` never executed. That block is the
  entire auto-compaction trigger and the ``/ctx`` token latch, so neither
  had ever run on the headless message path. The value ``arun`` returns
  (a ``RunOutput`` / ``TeamRunOutput``) does carry ``metrics``.

* **The session id.** Copied at construction. The handler is built once
  per ``Session`` and never rebuilt, while ``/clear`` and ``/fork`` go
  through ``SessionIdentity.rotate`` — so after the first ``/clear``,
  every audit row and every ``UserPromptSubmit`` / ``Stop`` hook payload
  carried the dead id, which is the id a hook author would filter on.

* **The context window.** Computed once in ``Session.__init__``.
  ``/model`` rebuilds the team around a different model and left the
  compaction threshold measuring against the old model's window.

The three are tested together because they are one habit: a collaborator
that outlives the state it copied.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ember_code.core.session.message_handler import SessionMessageHandler


class _Hooks:
    """Hook executor that always allows, recording every payload."""

    def __init__(self, stop_blocks: int = 0) -> None:
        self.payloads: list[tuple[str, dict]] = []
        self._stop_blocks = stop_blocks

    async def execute(self, *, event: str, payload: dict):
        self.payloads.append((event, payload))
        if event == "Stop" and self._stop_blocks > 0:
            self._stop_blocks -= 1
            return SimpleNamespace(should_continue=False, message="try again")
        return SimpleNamespace(should_continue=True, message="")

    def session_ids_for(self, event: str) -> list[str]:
        return [p["session_id"] for name, p in self.payloads if name == event]


class _Audit:
    def __init__(self) -> None:
        self.entries: list = []

    def log(self, entry) -> None:
        self.entries.append(entry)


class _Guardrails:
    async def warning_prefix(self, text: str) -> str:
        return ""


class _Team:
    """Records every ``arun`` and hands back a RunOutput-shaped value.

    Deliberately has **no** ``run_response`` attribute — matching the
    installed agno — so a handler that goes looking for one finds
    nothing, exactly as it did in production.
    """

    def __init__(self, input_tokens: list[int]) -> None:
        self._input_tokens = list(input_tokens)
        self.calls: list[str] = []

    async def arun(self, message: str, **kwargs):
        self.calls.append(message)
        tokens = self._input_tokens.pop(0) if self._input_tokens else 0
        return SimpleNamespace(
            status="completed",
            content="answer",
            metrics=SimpleNamespace(input_tokens=tokens),
        )


def _build(
    *,
    team: _Team,
    hooks: _Hooks,
    audit: _Audit,
    session_id_ref,
    context_window_ref,
    compact_calls: list,
    latched: list,
) -> SessionMessageHandler:
    return SessionMessageHandler(
        hook_executor=hooks,
        audit=audit,
        display=SimpleNamespace(print_error=lambda msg: None),
        guardrail_runner=_Guardrails(),
        team_ref=lambda: team,
        pending_reminders_drain=list,
        compact_hook=_recorder(compact_calls),
        ensure_mcp=_noop,
        session_id_ref=session_id_ref,
        context_window_ref=context_window_ref,
        latch_input_tokens=latched.append,
        extract_response_text=lambda resp: resp.content,
    )


def _recorder(sink: list):
    async def _compact(input_tokens: int, context_window: int) -> bool:
        sink.append((input_tokens, context_window))
        return False

    return _compact


async def _noop():
    return None


class TestTheTokenLatchAndCompactionActuallyFire:
    @pytest.mark.asyncio
    async def test_the_turns_input_tokens_reach_the_latch_and_the_compactor(self):
        """Reading ``team.run_response`` made this silently a no-op: both
        lists stayed empty on every turn, so ``/ctx`` reported a stale
        count and history grew unbounded past the compaction threshold."""
        compacted: list = []
        latched: list = []
        handler = _build(
            team=_Team([91_000]),
            hooks=_Hooks(),
            audit=_Audit(),
            session_id_ref=lambda: "s-1",
            context_window_ref=lambda: 128_000,
            compact_calls=compacted,
            latched=latched,
        )

        assert await handler.handle("hello") == "answer"

        assert latched == [91_000]
        assert compacted == [(91_000, 128_000)]

    @pytest.mark.asyncio
    async def test_a_stop_hook_retry_is_measured_not_the_superseded_run(self):
        """The retry re-runs the agent with the whole history plus a
        critique, so its input count is the larger one — and the only one
        the next threshold check can be right about."""
        compacted: list = []
        latched: list = []
        handler = _build(
            team=_Team([40_000, 44_000]),
            hooks=_Hooks(stop_blocks=1),
            audit=_Audit(),
            session_id_ref=lambda: "s-1",
            context_window_ref=lambda: 128_000,
            compact_calls=compacted,
            latched=latched,
        )

        await handler.handle("hello")

        assert latched == [44_000]
        assert compacted == [(44_000, 128_000)]


class TestTheHandlerReadsTheLiveSessionId:
    @pytest.mark.asyncio
    async def test_a_rotated_id_reaches_the_hooks_and_the_audit_log(self):
        """``/clear`` rotates the id mid-session. A by-value copy kept
        every later hook payload and audit row pointing at the session
        the user had already closed."""
        live = {"id": "before-clear"}
        hooks = _Hooks()
        audit = _Audit()
        handler = _build(
            team=_Team([10]),
            hooks=hooks,
            audit=audit,
            session_id_ref=lambda: live["id"],
            context_window_ref=lambda: 128_000,
            compact_calls=[],
            latched=[],
        )

        await handler.handle("first")
        live["id"] = "after-clear"
        await handler.handle("second")

        assert hooks.session_ids_for("UserPromptSubmit") == ["before-clear", "after-clear"]
        assert hooks.session_ids_for("Stop") == ["before-clear", "after-clear"]
        assert [e.session_id for e in audit.entries] == ["before-clear", "after-clear"]

    @pytest.mark.asyncio
    async def test_the_failure_path_uses_the_live_id_too(self):
        """The failure path is the one someone reads *after* the fact, so
        a stale id there costs the most."""

        class _Exploding(_Team):
            async def arun(self, message: str, **kwargs):
                raise RuntimeError("boom")

        live = {"id": "rotated"}
        hooks = _Hooks()
        audit = _Audit()
        handler = _build(
            team=_Exploding([]),
            hooks=hooks,
            audit=audit,
            session_id_ref=lambda: live["id"],
            context_window_ref=lambda: 128_000,
            compact_calls=[],
            latched=[],
        )

        assert "boom" in await handler.handle("go")

        assert [e.session_id for e in audit.entries] == ["rotated"]
        assert hooks.session_ids_for("StopFailure") == ["rotated"]


class TestTheHandlerReadsTheLiveContextWindow:
    @pytest.mark.asyncio
    async def test_a_model_switch_moves_the_compaction_threshold(self):
        """``/model`` from a 200k model to a 32k one used to leave the
        threshold at 200k — compaction waiting for a limit the new model
        blows past six times over."""
        window = {"n": 200_000}
        compacted: list = []
        handler = _build(
            team=_Team([50_000, 50_000]),
            hooks=_Hooks(),
            audit=_Audit(),
            session_id_ref=lambda: "s-1",
            context_window_ref=lambda: window["n"],
            compact_calls=compacted,
            latched=[],
        )

        await handler.handle("first")
        window["n"] = 32_000
        await handler.handle("second")

        assert compacted == [(50_000, 200_000), (50_000, 32_000)]


class TestTheSessionWiresTheRefsThrough:
    def test_session_passes_callables_not_copies(self):
        """The handler can only read live state if ``Session`` hands it
        callables — passing ``self.session_id`` reintroduces the whole
        defect with the handler unchanged."""
        import inspect

        from ember_code.core.session.core import Session

        source = inspect.getsource(Session.__init__)
        construction = source.split("SessionMessageHandler(")[1]

        assert "session_id_ref=lambda: self.session_id" in construction
        assert "context_window_ref=lambda: self._context_window" in construction

    def test_rebuilding_the_team_reresolves_the_window(self):
        """``/model`` reaches the window only through
        ``_rebuild_main_team`` — every model swap funnels there."""
        import inspect

        from ember_code.core.session.core import Session

        source = inspect.getsource(Session._rebuild_main_team)

        assert "self._context_window = self._resolve_context_window()" in source


class TestTheUserMemoryCapabilityIsGateable:
    """``update_user_memory`` is taught in six places in the main-agent
    prompt and attached only when the session has a LearningMachine *and*
    ``learning.user_memory`` is on — the same promise-a-tool-you-lack
    defect the ``knowledge-base`` spans were added for.

    The markdown spans that carry it live in ``core/prompts/``; what is
    pinned here is the half that decides them: the resolver knows the
    capability, and the agent builder computes it from the same two facts
    agno gates the tool on.
    """

    def _builder(self, *, user_memory: bool):
        from pathlib import Path

        from ember_code.core.session.agent_builder.prompt_builder import PromptBuilder

        return PromptBuilder(
            project_dir=Path("/nonexistent"),
            settings=SimpleNamespace(storage=SimpleNamespace(data_dir=Path("/nonexistent"))),
            codeindex_available=False,
            user_memory_available=user_memory,
            agent_catalog="",
            skill_descriptions="",
            prompt_loader=lambda name: "",
        )

    def test_the_span_is_cut_when_the_tool_is_not_attached(self):
        text = (
            "persist it with <!-- user-memory:only -->`update_user_memory`"
            "<!-- /user-memory:only --><!-- user-memory:absent -->nothing"
            "<!-- /user-memory:absent -->."
        )

        assert self._builder(user_memory=False).resolve_conditionals(text) == (
            "persist it with nothing."
        )
        assert self._builder(user_memory=True).resolve_conditionals(text) == (
            "persist it with `update_user_memory`."
        )

    def test_the_builder_reads_the_condition_agno_gates_on(self):
        """``learning.enabled=false``, ``user_memory=false`` and a session
        with no db all mean no tool; only the conjunction means it is
        there."""
        import inspect

        from ember_code.core.session.agent_builder.coordinator import MainAgentBuilder

        source = inspect.getsource(MainAgentBuilder.build_prompt)

        assert (
            "user_memory_available=s.learning is not None and s.settings.learning.user_memory"
            in source
        )

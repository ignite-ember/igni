"""Print mode must not depend on a human, a terminal, or a drained pipe.

``ember-code -p`` was built by reusing the interactive stack wholesale, and
kept three dependencies a terminal happens to satisfy and a pipe or file does
not. Each one produced a different symptom in the same headless run, which is
why they took so long to separate:

* a confirmation pause with nobody to answer it — the tool never ran and the
  placeholder ("I have tools to execute, but I need confirmation.") was printed
  as the assistant's answer, exit code 0;
* an httpcore DEBUG firehose on a stderr handler that alembic's ``fileConfig``
  installed on the root logger at every startup — against an undrained pipe the
  emit blocks inside httpcore on the event loop and the agent wedges at ~64 KB;
* ``sys.stdin.read()`` on a tty, which never reaches EOF, so ``-p -m "..."``
  hung before the session started.

Measured on a capture batch: sessions whose model calls had all completed in
seconds sat at the 1800s sub-agent spawn timeout. These tests pin the shape of
each fix rather than the symptom.
"""

from __future__ import annotations

import io
import logging
import os
import sys
import tempfile
from dataclasses import dataclass
from typing import Any

import pytest

from ember_code.core.config.settings import PermissionsConfig
from ember_code.core.config.tool_permissions import ToolPermissions
from ember_code.core.tools.tool_spec import ToolSpecCatalog


def _auto_approve_permissions() -> ToolPermissions:
    """What ``--auto-approve`` produces, via the same config object the
    CLI builds (``CliOverrides.from_options``)."""
    return ToolPermissions(
        settings_permissions=PermissionsConfig(
            mode="bypassPermissions",
            file_write="allow",
            shell_execute="allow",
            git_push="allow",
            git_destructive="allow",
        )
    )


# ── 1. --auto-approve means every tool, not the four mapped categories ──


def test_auto_approve_confirms_nothing_in_the_whole_catalog() -> None:
    """The bug this replaces: ``--auto-approve`` set four *category*
    fields, ``CategoryToToolMap`` knew three categories, and anything
    outside them kept the ``"ask"`` default with no flag able to flip it.
    ``CodeIndex`` and ``NotebookEdit`` are both in the main agent's
    toolkit and both were gated.

    Asserted over the entire catalog on purpose: a per-tool assertion
    would pass again the next time a tool is added outside the map.
    """
    permissions = _auto_approve_permissions()
    gated = sorted(
        name for name in ToolSpecCatalog.default().by_name if permissions.needs_confirmation(name)
    )
    assert gated == []


@pytest.mark.parametrize("tool", ["CodeIndex", "NotebookEdit"])
def test_the_two_tools_the_category_map_missed(tool: str) -> None:
    """Named explicitly so a regression points at the original defect
    instead of at a catalog-wide count."""
    assert _auto_approve_permissions().needs_confirmation(tool) is False


def test_notebook_edit_still_asks_by_default() -> None:
    """The bypass fix is scoped to the mode: a write tool with no flag
    set must still ask."""
    assert ToolPermissions().needs_confirmation("NotebookEdit") is True


def test_codeindex_is_a_read_tool_by_default() -> None:
    """CodeIndex fell through to the ``"ask"`` fallback purely because it
    was absent from the defaults table, not by intent — its one function
    is read-only Cypher behind ``assert_read_only_cypher``. It now sits
    with Grep, so it no longer needs a flag to be usable at all.
    """
    permissions = ToolPermissions()
    assert permissions.get_level("CodeIndex") == "allow"
    assert permissions.needs_confirmation("CodeIndex") is False


def test_the_codeindex_confirmation_gate_names_a_real_function() -> None:
    """Agno matches ``requires_confirmation_tools`` against the toolkit's
    actual functions and only *warns* on a miss, so four stale names left
    the gate silently dead. Verified against the live toolkit rather than
    a hardcoded list so the next rename fails here.
    """
    from ember_code.core.tools.codeindex.tool import CodeIndexTools
    from ember_code.core.tools.tool_spec import CodeIndexSpec

    real = {
        name
        for name in dir(CodeIndexTools)
        if name.startswith("codeindex_") and callable(getattr(CodeIndexTools, name))
    }
    stale = set(CodeIndexSpec().confirm_function_names) - real
    assert stale == set(), f"gate names functions the toolkit does not have: {stale}"


def test_bypass_mode_does_not_rewrite_levels() -> None:
    """The mode short-circuits confirmation only. ``get_level`` still
    reports the configured level, so the argument-level guards in
    ``PermissionEvaluator`` and any explicit ``deny`` rule keep working —
    a bypass silences the prompt, it does not grant everything.
    """
    permissions = _auto_approve_permissions()
    assert permissions.get_level("NotebookEdit") == "ask"
    assert permissions.needs_confirmation("NotebookEdit") is False


# ── 2. A spawned specialist inherits the user's permissions ─────────


def test_agent_builder_passes_settings_permissions_to_the_registry() -> None:
    """``AgentBuilder._resolve_tools`` constructed ``ToolPermissions``
    with ``project_dir`` only, so every specialist got pure defaults —
    ``save_file`` / ``edit_file`` / ``run_shell_command`` all at ``"ask"``
    — regardless of what the user asked for. Headless, the specialist's
    first write paused and ``SubAgentHITLCoordinator.wait_resolved``
    blocked until the 1800s spawn timeout.
    """
    from ember_code.core.agents import builder as builder_module

    seen: dict[str, Any] = {}

    class RecordingToolPermissions(ToolPermissions):
        def __init__(self, **kwargs: Any) -> None:
            seen.update(kwargs)
            super().__init__(**kwargs)

    class StubRegistry:
        def __init__(self, **_: Any) -> None:
            pass

        def resolve(self, _names: Any) -> list[Any]:
            return []

    class StubSettings:
        # The real config object, not a stub: ``iter_config_levels`` reads every
        # category field, so a stub only proves the stub's shape.
        permissions = PermissionsConfig(mode="bypassPermissions", file_write="allow")

    @dataclass
    class StubContext:
        settings: Any
        base_dir: str | None = None
        mcp_clients: dict[str, Any] | None = None
        knowledge_mgr: Any = None
        db: Any = None
        broadcast: Any = None
        code_index_provider: Any = None

    @dataclass
    class StubDefinition:
        tools: list[str]

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(builder_module, "ToolPermissions", RecordingToolPermissions)
        monkey.setattr(
            builder_module.AgentBuilder, "_tool_registry_cls", StubRegistry, raising=False
        )
        builder = builder_module.AgentBuilder(StubContext(settings=StubSettings()))
        builder._resolve_tools(StubDefinition(tools=["Write", "Bash"]))
    finally:
        monkey.undo()

    assert "settings_permissions" in seen, (
        "the specialist's registry was built without the user's permissions"
    )
    assert getattr(seen["settings_permissions"], "mode", None) == "bypassPermissions"


# ── 3. A pause is an error, never an answer ─────────────────────────


def test_paused_run_raises_instead_of_being_returned_as_the_answer() -> None:
    """``grep RunStatus.paused src/`` found nothing outside ``backend/``:
    pause handling lived only behind the React client's RPC. On this path
    the placeholder content was extracted and printed as the response.
    """
    from ember_code.core.session.message_handler import SessionMessageHandler

    class PausedToolCall:
        tool_name = "codeindex_search"
        is_paused = True

    class PausedResponse:
        status = "paused"
        content = "I have tools to execute, but I need confirmation."
        tools = [PausedToolCall()]

    with pytest.raises(RuntimeError) as excinfo:
        SessionMessageHandler._reject_paused_run(PausedResponse())
    assert "codeindex_search" in str(excinfo.value)


def test_a_normal_run_is_left_alone() -> None:
    from ember_code.core.session.message_handler import SessionMessageHandler

    class CompletedResponse:
        status = "completed"
        content = "done"
        tools: list[Any] = []

    SessionMessageHandler._reject_paused_run(CompletedResponse())
    SessionMessageHandler._reject_paused_run(object())  # no status attribute at all


# ── 4. Startup leaves no stderr handler for the firehose to fill ────


def test_database_startup_does_not_seize_the_root_logger() -> None:
    """``Database.__init__`` runs alembic on every startup, and
    ``env.py``'s ``fileConfig`` reconfigured the *root* logger from
    ``alembic.ini`` — installing a NOTSET stderr handler and deleting
    whatever the application had put there (which silently redirected
    ``--debug``'s file handler to stderr).
    """
    from ember_code.core.db.database import Database

    sentinel = logging.NullHandler()
    logging.root.addHandler(sentinel)
    captured, real_stderr = io.StringIO(), sys.stderr
    try:
        sys.stderr = captured
        Database(db_path=os.path.join(tempfile.mkdtemp(), "startup.db"))
        surviving = list(logging.root.handlers)
    finally:
        sys.stderr = real_stderr
        logging.root.removeHandler(sentinel)

    assert sentinel in surviving, "alembic deleted the app's root handlers"
    stderr_handlers = [
        handler
        for handler in surviving
        if isinstance(handler, logging.StreamHandler)
        and getattr(handler, "stream", None) in (real_stderr, captured)
    ]
    assert stderr_handlers == []
    assert captured.getvalue() == "", f"startup wrote to stderr: {captured.getvalue()!r}"


def test_transport_loggers_do_not_propagate_to_root() -> None:
    """``LlmCallLogger`` sets httpx/httpcore to DEBUG for connection
    diagnostics. Those records are meant for ``~/.igni/llm_calls.log``;
    ``callHandlers`` checks each *handler's* level and never the ancestor
    loggers', so while they propagated, root's WARNING did not filter
    them and every one reached whatever handler root had.
    """
    from ember_code.core.config.llm_call_logger import LlmCallLogger

    previous = {name: logging.getLogger(name).propagate for name in ("httpx", "httpcore")}
    try:
        LlmCallLogger()._attach_transport_loggers(logging.NullHandler())
        for name in ("httpx", "httpcore"):
            assert logging.getLogger(name).propagate is False, name
    finally:
        for name, value in previous.items():
            logging.getLogger(name).propagate = value


# ── 5. -m does not wait on a keyboard ───────────────────────────────


def test_pipe_mode_with_m_does_not_read_a_tty() -> None:
    """``read_pipe_message`` read stdin unconditionally and only then
    looked at ``-m``, so ``-p -m "..."`` from a terminal blocked in
    ``read()`` forever with zero bytes on either stream.
    """
    from ember_code.cli.invocation import CliInvocation

    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

        def read(self, *_: Any) -> str:  # pragma: no cover - must not be called
            raise AssertionError("read a tty stdin; this is the hang")

    router = CliInvocation.__new__(CliInvocation)
    router._options = type("Opts", (), {"message": "list the exports of foo.py"})()

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(sys, "stdin", Tty())
        assert router.read_pipe_message() == "list the exports of foo.py"
    finally:
        monkey.undo()


def test_a_real_pipe_is_still_read_and_combined() -> None:
    """The fix is scoped to a tty: genuine ``cat file | ember -p -m "..."``
    must still concatenate both."""
    from ember_code.cli.invocation import CliInvocation

    class Pipe(io.StringIO):
        def isatty(self) -> bool:
            return False

    router = CliInvocation.__new__(CliInvocation)
    router._options = type("Opts", (), {"message": "summarise this"})()

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(sys, "stdin", Pipe("piped body\n"))
        assert router.read_pipe_message() == "summarise this\n\npiped body"
    finally:
        monkey.undo()


# ── 6. The turn's leftovers cannot hold the process open ────────────


def test_drain_cancels_leftover_tasks_and_returns() -> None:
    """``asyncio.run``'s close path is unbounded in two places
    (``_cancel_all_tasks`` gathers with no timeout;
    ``shutdown_default_executor`` leaves a non-daemon thread the
    interpreter then joins forever), so a session that leaks a task had
    no way to exit. Draining inside the loop, with a deadline, keeps the
    leftovers away from it.

    There is always something to leak here: a shell command slower than
    ``run_shell_command``'s 7s default is auto-backgrounded and never
    killed in this mode.
    """
    import asyncio

    from ember_code.cli import _run_then_drain

    leaked: list[asyncio.Task[None]] = []

    async def scenario() -> None:
        async def forever() -> None:
            await asyncio.sleep(3600)

        leaked.append(asyncio.create_task(forever()))
        await asyncio.sleep(0)  # let it start

    asyncio.run(_run_then_drain(scenario()))
    assert leaked and leaked[0].cancelled()


def test_drain_gives_up_on_a_task_that_ignores_cancellation() -> None:
    """The deadline is the point: a task that swallows
    ``CancelledError`` must not be able to keep the process alive. It is
    abandoned, not waited on."""
    import asyncio

    from ember_code.cli import _run_then_drain

    async def scenario() -> None:
        async def stubborn() -> None:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                await asyncio.sleep(3600)  # refuses to die

        asyncio.create_task(stubborn())
        await asyncio.sleep(0)

    import ember_code.cli as cli_module

    original = cli_module._DRAIN_TIMEOUT_SECONDS
    cli_module._DRAIN_TIMEOUT_SECONDS = 0.05
    try:
        asyncio.run(_run_then_drain(scenario()))  # returns rather than hanging
    finally:
        cli_module._DRAIN_TIMEOUT_SECONDS = original


def test_the_body_logger_does_not_propagate_either() -> None:
    """``ember_code.llm_calls`` writes full request *and* response
    bodies — the reason llm_calls.log reaches hundreds of MB. Fixing only
    httpx/httpcore left the larger producer pointed at root, which is the
    same wedge with more bytes behind it.
    """
    import logging

    from ember_code.core.config.llm_call_logger import LlmCallLogger

    logger = logging.getLogger("ember_code.llm_calls")
    previous_propagate, previous_handlers = logger.propagate, list(logger.handlers)
    try:
        logger.handlers.clear()
        LlmCallLogger().ensure_configured()
        assert logger.propagate is False
    finally:
        logger.handlers[:] = previous_handlers
        logger.propagate = previous_propagate


# ── 7. CodeIndex actually has a backend behind it ───────────────────


def test_the_toolkit_uses_the_sessions_index_not_a_self_built_one() -> None:
    """``CodeIndexTools`` self-builds a ``CodeIndex`` when given nothing,
    and a self-built one has no ``runtime=`` — so ``client_for`` returns
    None and every query answers ``no_backend``. Nothing in the codebase
    passed ``index=``, which made that the behaviour on every path, not
    only the headless one. Verified live afterwards: 4,845 Item nodes.
    """
    from ember_code.core.tools.registry import ToolRegistry

    sentinel = object()
    registry = ToolRegistry(base_dir="/tmp", code_index_provider=lambda: sentinel)
    (toolkit,) = registry.resolve(["CodeIndex"])
    assert toolkit._services.index is sentinel


def test_the_index_is_resolved_lazily_not_at_construction() -> None:
    """The provider is a callable rather than the index itself because
    ``attach_codeindex_neo4j`` *replaces* ``session.code_index`` after the
    team — and therefore this toolkit — is built. Capturing the object
    would pin the runtime-less startup index.
    """
    from ember_code.core.tools.registry import ToolRegistry

    calls: list[int] = []
    current = [object()]

    def provider() -> object:
        calls.append(1)
        return current[0]

    registry = ToolRegistry(base_dir="/tmp", code_index_provider=provider)
    (toolkit,) = registry.resolve(["CodeIndex"])
    assert calls == [], "provider was called at build time; the swap would be missed"

    replacement = object()
    current[0] = replacement  # what attach_codeindex_neo4j does
    assert toolkit._services.index is replacement


def test_single_message_run_releases_only_what_it_took() -> None:
    """``stop_all`` ignores refcounts and kills every active pair, which
    is correct at backend shutdown and wrong for one CLI run — a backend
    or second CLI on the same pair would lose its server. Release must be
    the matching ``stop_for_commit``.
    """
    import asyncio

    from ember_code.core.session.single_message_run import SingleMessageRun

    class Runtime:
        def __init__(self) -> None:
            self.released: list[tuple[str, str]] = []
            self.stopped_all = False

        async def stop_for_commit(self, project: str, sha: str) -> bool:
            self.released.append((project, sha))
            return True

        async def stop_all(self) -> None:
            self.stopped_all = True

    run = SingleMessageRun.__new__(SingleMessageRun)
    run._started_commit = ("proj-hash", "deadbeef")
    runtime = Runtime()
    asyncio.run(run._release_neo4j(runtime))

    assert runtime.released == [("proj-hash", "deadbeef")]
    assert runtime.stopped_all is False


def test_nothing_is_released_when_nothing_was_started() -> None:
    import asyncio

    from ember_code.core.session.single_message_run import SingleMessageRun

    class Runtime:
        async def stop_for_commit(self, *_: str) -> bool:  # pragma: no cover
            raise AssertionError("released a commit this run never started")

        async def stop_all(self) -> None:  # pragma: no cover
            raise AssertionError("stop_all is never correct here")

    run = SingleMessageRun.__new__(SingleMessageRun)
    asyncio.run(run._release_neo4j(Runtime()))  # no _started_commit set
    asyncio.run(run._release_neo4j(None))


def test_keep_alive_leaves_the_pair_running() -> None:
    """Batch callers running many single-shot sessions over one repo can
    opt out of the release: a cold Neo4j start measured 44s end-to-end
    against 5-8s attaching to a warm server, so releasing between every
    session costs hours across a large campaign."""
    import asyncio

    from ember_code.core.session.single_message_run import SingleMessageRun

    class Runtime:
        async def stop_for_commit(self, *_: str) -> bool:  # pragma: no cover
            raise AssertionError("released despite IGNI_NEO4J_KEEP_ALIVE")

        async def stop_all(self) -> None:  # pragma: no cover
            raise AssertionError("stop_all is never correct here")

    run = SingleMessageRun.__new__(SingleMessageRun)
    run._started_commit = ("proj", "sha")

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setenv("IGNI_NEO4J_KEEP_ALIVE", "1")
        asyncio.run(run._release_neo4j(Runtime()))
    finally:
        monkey.undo()


def test_head_commit_is_noted_without_acquiring() -> None:
    """``_client_for`` already calls ``start_for_commit`` on first query
    and caches per sha, so acquiring here too made two acquires against
    one release — ``stop_for_commit`` then reported "still attached by 1
    other BE(s)" and the server outlived every run."""
    import asyncio

    from ember_code.core.session.single_message_run import SingleMessageRun

    class Index:
        project_id = "proj"

        def has_commit(self, _sha: str) -> bool:
            return True

    class Sync:
        def current_sha(self) -> str:
            return "sha123"

    class Runtime:
        async def start_for_commit(self, *_: str) -> None:  # pragma: no cover
            raise AssertionError("acquired eagerly; the lazy acquire already counts")

    class Session:
        code_index = Index()
        code_index_sync = Sync()

    run = SingleMessageRun.__new__(SingleMessageRun)
    run._session = Session()
    asyncio.run(run._note_head_commit())
    assert run._started_commit == ("proj", "sha123")


# ── 8. An abbreviated commit must not become a second, empty store ──


def _index_with_commits(*commits: str):
    """A CodeIndex stub exposing just the manifest surface
    ``_resolve_indexed_commit`` reads."""
    from ember_code.core.code_index.index import CodeIndex

    class Manifest:
        def load(self) -> Any:
            return type("M", (), {"commits": list(commits)})()

    index = CodeIndex.__new__(CodeIndex)
    index.manifest = Manifest()
    return index


FULL_SHA = "84a9f3b9a4f3244b8c8e818f557d64c7b964fb25"


def test_abbreviated_commit_resolves_to_the_indexed_one() -> None:
    """Agents pass the short sha they saw in conversation. Because
    ``(project, commit)`` is a *store identity* rather than a lookup, the
    abbreviation used to spawn a second Neo4j on an empty store: the
    query returned zero rows and the agent reported the index as empty.
    Observed live — two servers for one project, one keyed by the 40-char
    sha and one by 7 chars.
    """
    index = _index_with_commits(FULL_SHA)
    assert index._resolve_indexed_commit("84a9f3b") == FULL_SHA
    assert index._resolve_indexed_commit(FULL_SHA) == FULL_SHA


def test_an_unindexed_commit_is_refused_not_invented() -> None:
    """A silent empty graph is worse than an error: a corpus built on it
    teaches that the index has no data. ``cypher_service`` turns this into
    a ``client_for_failed`` the agent can act on."""
    index = _index_with_commits(FULL_SHA)
    with pytest.raises(ValueError, match="not indexed"):
        index._resolve_indexed_commit("deadbeef")


def test_an_empty_commit_falls_back_to_head_not_the_resolver() -> None:
    """``client_for``'s ``sha or self.head()`` means an empty string never
    reaches the resolver — worth pinning, because a ``<project>-`` directory
    with no sha exists on disk, so *some* path did once open an empty commit.
    Not this one."""
    index = _index_with_commits(FULL_SHA)
    # "" matches every commit as a prefix, so reaching the resolver with it
    # would silently pick an arbitrary store rather than raising.
    assert index._resolve_indexed_commit("") == FULL_SHA


def test_an_ambiguous_prefix_is_refused() -> None:
    index = _index_with_commits("abc111" + "0" * 34, "abc222" + "0" * 34)
    with pytest.raises(ValueError, match="ambiguous"):
        index._resolve_indexed_commit("abc")

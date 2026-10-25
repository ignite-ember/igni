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
    ``NotebookEdit`` is in the main agent's toolkit and was gated.

    Asserted over the entire catalog on purpose: a per-tool assertion
    would pass again the next time a tool is added outside the map.
    """
    permissions = _auto_approve_permissions()
    gated = sorted(
        name for name in ToolSpecCatalog.default().by_name if permissions.needs_confirmation(name)
    )
    assert gated == []


def test_the_tool_the_category_map_missed() -> None:
    """Named explicitly so a regression points at the original defect
    instead of at a catalog-wide count."""
    assert _auto_approve_permissions().needs_confirmation("NotebookEdit") is False


def test_notebook_edit_still_asks_by_default() -> None:
    """The bypass fix is scoped to the mode: a write tool with no flag
    set must still ask."""
    assert ToolPermissions().needs_confirmation("NotebookEdit") is True


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
        artifact_mgr: Any = None
        db: Any = None
        broadcast: Any = None

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
        tool_name = "web_search"
        is_paused = True

    class PausedResponse:
        status = "paused"
        content = "I have tools to execute, but I need confirmation."
        tools = [PausedToolCall()]

    with pytest.raises(RuntimeError) as excinfo:
        SessionMessageHandler._reject_paused_run(PausedResponse())
    assert "web_search" in str(excinfo.value)


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

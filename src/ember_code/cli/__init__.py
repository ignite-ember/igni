"""igni CLI — the ``ember`` command-line entry point.

Non-interactive command surface. The Textual TUI was removed in
v0.9.7 — interactive chat now lives in the React clients
(``clients/web``, ``clients/tauri``, ``clients/vscode``,
``clients/jetbrains``), which speak to the backend server started
via ``python -m ember_code.backend --socket <path>``.

Supported modes:

* ``ember -m "<message>"`` — single-message non-interactive run.
* ``ember -p`` — pipe mode: read stdin, run one message, write
  stdout.
* ``ember`` (no args) — prints help and pointer to the React
  clients; use ``python -m ember_code.backend --socket <path>`` to
  start the backend for a client to connect to.

Global flags (permissions, worktree, additional dirs) apply to
the non-interactive modes.

This module is a *thin* Click-decorator surface: every option
lands on :func:`cli`, which validates the params into a typed
:class:`CliOptions`, loads settings via
:class:`CliOverrides.from_options`, and hands off to
:class:`CliInvocation` for the real work. Keeping the decorator
stack here (as opposed to the invocation module) lets tests keep
patching ``ember_code.cli.asyncio.run`` — the mode-dispatch tail
that runs the async session helpers lives here.
"""

from __future__ import annotations

import asyncio

# Aliased: importing the ``ember_code.cli.logging`` submodule binds it as an
# attribute of this package, which would shadow a plain ``logging`` global here.
import logging as stdlib_logging
import os
import sys
import threading as stdlib_threading
import traceback
from typing import Any

import click

from ember_code import __version__
from ember_code.cli.invocation import (
    CliInvocation,
    load_settings_from_options,
)
from ember_code.cli.options import CliOptions
from ember_code.core import session as _session_module
from ember_code.core.paths import DEFAULT_DATA_DIR

logger = stdlib_logging.getLogger(__name__)


# A plain command, not a group. This was ``@click.group`` with zero
# commands registered — nothing anywhere calls ``add_command`` and nothing
# is decorated with ``@cli.command`` — so Click put ``[COMMAND] [ARGS]...``
# in the usage line advertising something that did not exist. Every
# natural first move after reading ``--help`` (``igni help``,
# ``igni login``, ``igni chat``) produced a usage error instead.
@click.command()
@click.version_option(version=__version__, prog_name="igni")
@click.option("--model", default=None, help="Model to use")
@click.option("--verbose", is_flag=True, help="Show routing and reasoning")
@click.option("--quiet", is_flag=True, help="Minimal output")
@click.option("-m", "--message", default=None, help="Single message (non-interactive)")
@click.option(
    "--continue", "-c", "continue_session", is_flag=True, help="Resume the most recent session"
)
@click.option("--session-id", default=None, help="Resume a specific session by ID")
@click.option("--read-only", is_flag=True, help="No file modifications")
@click.option("--accept-edits", is_flag=True, help="Auto-approve file edits")
@click.option("--auto-approve", is_flag=True, help="Auto-approve everything")
@click.option(
    "-p", "--pipe", is_flag=True, help="Pipe mode: read stdin, write stdout, no interactive UI"
)
@click.option("--no-web", is_flag=True, help="Disable web search/fetch tools")
@click.option("--no-color", is_flag=True, help="Disable color output")
@click.option("--debug", is_flag=True, help=f"Enable debug logging to {DEFAULT_DATA_DIR}/debug.log")
@click.option(
    "--strict",
    is_flag=True,
    # Not "deny all dangerous operations": hooks run shell commands on a
    # separate path that no permission gates, so a group-supplied hook
    # still executes here. See
    # tests/test_hooks_are_outside_the_permission_ratchet.py.
    help="Deny the agent's dangerous operations (writes, shell, git push)",
)
@click.option("--worktree", is_flag=True, help="Run in an isolated git worktree")
@click.option(
    "--add-dir",
    multiple=True,
    type=click.Path(exists=True, file_okay=False),
    help="Additional directory to include (can be repeated)",
)
@click.pass_context
def cli(ctx: click.Context, **_params: object) -> None:
    """igni — an AI coding assistant that works on your code where it lives.

    Run with no arguments for an interactive session, or use -m to ask a
    single question and exit.
    """
    # Click prints the docstring above verbatim as ``--help`` output, so
    # implementation notes do not belong in it. This one used to explain
    # the delegation to ``CliInvocation`` and every user saw it, complete
    # with a ``:class:`` reStructuredText marker Click does not render.
    #
    # The note itself is worth keeping: Click forces a
    # decorator-per-option surface here, so all the logic lives on the
    # invocation object and this callback stays small.
    options = CliOptions.model_validate(ctx.params)
    settings = load_settings_from_options(options)

    invocation = CliInvocation(options, settings, ctx)
    invocation.enable_debug_logging()

    invocation.resolve_resume_id()
    invocation.setup_worktree()
    invocation.resolve_additional_dirs()
    invocation.store_context()

    if options.pipe:
        _run_pipe(invocation)
        return
    if options.message:
        _run_single_message(invocation, options.message)
        return

    # No message + no subcommand → point users at the React clients.
    invocation.echo_help_pointer()
    invocation.cleanup_worktree()


#: How long the drain below waits for leftover tasks before giving up on them.
_DRAIN_TIMEOUT_SECONDS = 5.0


async def _run_then_drain(coro: Any) -> None:
    """Await ``coro``, then cancel and drain whatever it left behind.

    Draining here, inside the loop, is what keeps the leftovers away from
    ``asyncio.run``'s own close path — which is unbounded in two places:
    ``_cancel_all_tasks`` gathers the leftovers with no timeout, and
    ``shutdown_default_executor`` joins the executor's threads with a 300s
    default and then leaves a *non-daemon* ``_do_shutdown`` thread behind
    when that expires. Nothing bounds the interpreter's join of that thread,
    which is the "sat there forever" half.

    There is plenty to leave behind: a shell command slower than
    ``run_shell_command``'s 7s default is auto-backgrounded and never killed
    in this mode, and ``background: true`` hooks are fire-and-forget tasks.
    """
    try:
        await coro
    finally:
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            for task in pending:
                task.cancel()
            # Bounded on purpose: a task that ignores cancellation must not be
            # able to hold the process open. Anything still running is dropped
            # by the exit below.
            await asyncio.wait(pending, timeout=_DRAIN_TIMEOUT_SECONDS)


def _run_session(invocation: CliInvocation, coro: Any) -> None:
    """Run one non-interactive session and then actually exit.

    Measured before this existed: sessions whose every model call had
    finished in seconds sat until a 240s external timeout — and without one,
    indefinitely. The work was done; the process simply could not get out of
    interpreter shutdown (see :func:`_run_then_drain`).

    This mode has no teardown of its own — the 5-step ``ShutdownPipeline``
    that closes shells, MCP, LSP and monitors is reached only from
    ``backend/server.py``. Until a session-owned ``aclose()`` exists, the
    honest thing for a CLI whose work is provably finished is to flush and
    go, rather than block on joining threads nobody will ever stop.
    """
    code = 0
    try:
        asyncio.run(_run_then_drain(coro))
    except SystemExit as exc:  # a hook or handler asked for a specific code
        code = int(exc.code or 0)
    except BaseException:  # noqa: BLE001 — report, then still exit cleanly
        traceback.print_exc()
        code = 1

    try:
        invocation.cleanup_worktree()
    except Exception:  # noqa: BLE001 — cleanup must not keep the process alive
        logger.debug("worktree cleanup failed on exit", exc_info=True)

    sys.stdout.flush()
    sys.stderr.flush()

    # Only skip interpreter shutdown when a normal exit would actually be at
    # risk. ``threading._shutdown`` and
    # ``concurrent.futures.thread._python_exit`` join *non-daemon* threads with
    # no timeout, so surviving ones are precisely the hazard — most often the
    # ``_do_shutdown`` thread left stuck in ``executor.shutdown(wait=True)``.
    # With none of them the normal path is safe, which also keeps the in-process
    # CLI tests (11 of them patch ``asyncio.run`` and call straight through
    # here) from being killed mid-suite.
    lingering = [
        thread
        for thread in stdlib_threading.enumerate()
        if thread is not stdlib_threading.main_thread() and not thread.daemon
    ]
    if lingering:
        logger.debug("hard-exiting past %d non-daemon thread(s)", len(lingering))
        os._exit(code)
    if code:
        raise SystemExit(code)


def _run_pipe(invocation: CliInvocation) -> None:
    """Read stdin + ``-m`` and route through the session runner."""
    text = invocation.read_pipe_message()
    _run_session(
        invocation,
        _session_module.run_single_message(
            invocation.settings,
            text,
            resume_session_id=invocation.resume_session_id,
            project_dir=invocation.project_dir,
            additional_dirs=invocation.additional_dirs,
        ),
    )


def _run_single_message(invocation: CliInvocation, message: str) -> None:
    """Route a ``-m`` message through the session runner."""
    _run_session(
        invocation,
        _session_module.run_single_message(
            invocation.settings,
            message,
            resume_session_id=invocation.resume_session_id,
            project_dir=invocation.project_dir,
            additional_dirs=invocation.additional_dirs,
        ),
    )


__all__ = ["cli"]


if __name__ == "__main__":
    cli()

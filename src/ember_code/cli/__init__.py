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

import click

from ember_code import __version__
from ember_code.cli.invocation import (
    CliInvocation,
    load_settings_from_options,
)
from ember_code.cli.options import CliOptions
from ember_code.core import session as _session_module


@click.group(invoke_without_command=True)
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
@click.option("--debug", is_flag=True, help="Enable debug logging to ~/.ember/debug.log")
@click.option("--strict", is_flag=True, help="Strict mode: deny all dangerous operations")
@click.option("--worktree", is_flag=True, help="Run in an isolated git worktree")
@click.option(
    "--add-dir",
    multiple=True,
    type=click.Path(exists=True, file_okay=False),
    help="Additional directory to include (can be repeated)",
)
@click.pass_context
def cli(ctx: click.Context, **_params: object) -> None:
    """igni — AI coding assistant powered by Agno.

    Body deliberately delegates to :class:`CliInvocation` — Click
    forces a decorator-per-option surface here, but every ounce of
    logic lives on the invocation object so the callback stays
    small and readable.
    """
    options = CliOptions.model_validate(ctx.params)
    settings = load_settings_from_options(options)

    invocation = CliInvocation(options, settings, ctx)
    invocation.enable_debug_logging()

    # Subcommand invoked — the group callback stops here so the
    # subcommand handler owns the rest of the dispatch. Store just
    # enough on ``ctx.obj`` for subcommands to find the settings.
    if ctx.invoked_subcommand is not None:
        invocation.store_context()
        return

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


def _run_pipe(invocation: CliInvocation) -> None:
    """Read stdin + ``-m`` and route through the session runner."""
    text = invocation.read_pipe_message()
    asyncio.run(
        _session_module.run_single_message(
            invocation.settings,
            text,
            resume_session_id=invocation.resume_session_id,
            project_dir=invocation.project_dir,
            additional_dirs=invocation.additional_dirs,
        )
    )
    invocation.cleanup_worktree()


def _run_single_message(invocation: CliInvocation, message: str) -> None:
    """Route a ``-m`` message through the session runner."""
    asyncio.run(
        _session_module.run_single_message(
            invocation.settings,
            message,
            resume_session_id=invocation.resume_session_id,
            project_dir=invocation.project_dir,
            additional_dirs=invocation.additional_dirs,
        )
    )
    invocation.cleanup_worktree()


__all__ = ["cli"]


if __name__ == "__main__":
    cli()

"""CLI interface for igni."""

import asyncio

import click

from ember_code import __version__


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
    "--no-tui", is_flag=True, default=False, help="Use plain Rich CLI instead of Textual TUI"
)
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
def cli(
    ctx,
    model,
    verbose,
    quiet,
    message,
    continue_session,
    session_id,
    read_only,
    accept_edits,
    auto_approve,
    no_tui,
    pipe,
    no_web,
    no_color,
    debug,
    strict,
    worktree,
    add_dir,
):
    """igni — AI coding assistant powered by Agno."""
    ctx.ensure_object(dict)

    # ── Debug logging ──────────────────────────────────────────────
    if debug:
        import logging
        from logging.handlers import RotatingFileHandler
        from pathlib import Path

        log_path = Path.home() / ".ember" / "debug.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            str(log_path),
            maxBytes=10_000_000,
            backupCount=2,  # 10MB, keep 2 old
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
        logging.root.handlers.clear()
        logging.root.addHandler(handler)
        logging.root.setLevel(logging.DEBUG)
        logging.getLogger("ember_code").setLevel(logging.DEBUG)
        click.echo(f"Debug logging enabled → {log_path}")

    # Build CLI overrides
    cli_overrides = {}
    if model:
        cli_overrides.setdefault("models", {})["default"] = model
    if verbose:
        cli_overrides.setdefault("display", {}).update(
            {
                "show_routing": True,
                "show_reasoning": True,
            }
        )
    if quiet:
        cli_overrides.setdefault("display", {}).update(
            {
                "show_tool_calls": False,
                "show_routing": False,
            }
        )
    if read_only:
        cli_overrides.setdefault("permissions", {}).update(
            {
                "file_write": "deny",
                "shell_execute": "deny",
            }
        )
    if accept_edits:
        cli_overrides.setdefault("permissions", {})["file_write"] = "allow"
    if auto_approve:
        cli_overrides.setdefault("permissions", {}).update(
            {
                "file_write": "allow",
                "shell_execute": "allow",
                "git_push": "allow",
                "git_destructive": "allow",
            }
        )
    if no_web:
        cli_overrides.setdefault("permissions", {}).update(
            {
                "web_search": "deny",
                "web_fetch": "deny",
            }
        )
    if strict:
        cli_overrides.setdefault("permissions", {}).update(
            {
                "file_write": "deny",
                "shell_execute": "deny",
                "git_push": "deny",
                "git_destructive": "deny",
            }
        )

    # Load settings
    from ember_code.core.config.settings import load_settings

    settings = load_settings(cli_overrides=cli_overrides if cli_overrides else None)
    ctx.obj["settings"] = settings

    if ctx.invoked_subcommand is not None:
        return

    # Determine resume session id
    resume_session_id = None
    if session_id:
        resume_session_id = session_id
    elif continue_session:
        from ember_code.core.config.settings import load_settings as _ls
        from ember_code.core.memory.manager import setup_db
        from ember_code.core.session.persistence import SessionPersistence

        _s = _ls(cli_overrides=cli_overrides if cli_overrides else None)
        _db = setup_db(_s)
        _p = SessionPersistence(_db, session_id="")
        try:
            sessions = asyncio.run(_p.list_sessions(limit=1))
            if sessions:
                resume_session_id = sessions[0]["session_id"]
                click.echo(f"Resuming last session: {resume_session_id}")
            else:
                click.echo("No previous sessions found.")
        except Exception:
            click.echo("Could not look up last session.")

    # ── Worktree setup ───────────────────────────────────────────
    project_dir = None
    worktree_manager = None

    if worktree:
        from pathlib import Path

        from ember_code.core.worktree import WorktreeManager

        wm = WorktreeManager(Path.cwd())
        wt_info = wm.create(session_id=resume_session_id)
        project_dir = wt_info.worktree_path
        worktree_manager = wm
        click.echo(f"Worktree: {wt_info.worktree_path} (branch: {wt_info.branch_name})")

    # ── Additional directories ───────────────────────────────────
    additional_dirs = None
    if add_dir:
        from pathlib import Path

        additional_dirs = [Path(d).resolve() for d in add_dir]

    # ── Store for cleanup ────────────────────────────────────────
    ctx.obj["worktree_manager"] = worktree_manager
    ctx.obj["project_dir"] = project_dir
    ctx.obj["additional_dirs"] = additional_dirs

    # -- Pipe mode --
    if pipe:
        import sys

        text = sys.stdin.read().strip()
        if message:
            text = f"{message}\n\n{text}" if text else message
        if not text:
            click.echo("Error: no input provided via stdin or -m", err=True)
            raise SystemExit(1)
        from ember_code.core.session import run_single_message

        asyncio.run(
            run_single_message(
                settings,
                text,
                resume_session_id=resume_session_id,
                project_dir=project_dir,
                additional_dirs=additional_dirs,
            )
        )
        _worktree_cleanup(worktree_manager)
        return

    # -- Single message mode (non-interactive) --
    if message:
        from ember_code.core.session import run_single_message

        asyncio.run(
            run_single_message(
                settings,
                message,
                resume_session_id=resume_session_id,
                project_dir=project_dir,
                additional_dirs=additional_dirs,
            )
        )
        _worktree_cleanup(worktree_manager)
        return

    # -- Interactive mode (TUI only for now) --
    if no_tui:
        click.echo("Error: --no-tui mode is temporarily disabled. Use TUI mode (default).")
        click.echo("This will be revisited in a future release.")
        raise SystemExit(1)
    else:
        from ember_code.frontend.tui import EmberApp

        app = EmberApp(
            settings=settings,
            resume_session_id=resume_session_id,
            project_dir=project_dir,
            additional_dirs=additional_dirs,
            debug=debug,
        )
        _run_app(app)
        _worktree_cleanup(worktree_manager)


def _run_app(app):
    """Run the Textual app."""
    app.run()


def _worktree_cleanup(wm) -> None:
    """Clean up worktree after session ends. Report if changes exist."""
    if wm is None:
        return
    info = wm.info
    if info is None:
        return
    cleaned = wm.cleanup()
    if not cleaned:
        click.echo("\nWorktree preserved (has changes):")
        click.echo(f"  Path:   {info.worktree_path}")
        click.echo(f"  Branch: {info.branch_name}")
        click.echo(f"\nTo merge: git merge {info.branch_name}")
        click.echo(f"To remove: git worktree remove {info.worktree_path}")
    else:
        click.echo("Worktree cleaned up (no changes).")


if __name__ == "__main__":
    cli()

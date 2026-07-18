"""CLI invocation orchestrator.

Owns every ounce of behaviour the pre-refactor procedural
callback used to run inline: debug logging, resume-id lookup,
worktree setup, additional-dirs resolution, and mode dispatch
(pipe / single-message / help-pointer). The Click decorator
surface in :mod:`ember_code.cli.__init__` collapses to three
lines that build a :class:`CliOptions`, load settings, and
delegate here.

Test seams (preserved intentionally):

* ``_settings_module.load_settings`` is called via the module
  attribute so ``tests/test_cli.py`` can patch
  ``ember_code.core.config.settings.load_settings`` and have the
  CLI observe the mock. This is the ONE surviving module-level
  function on :mod:`core.config.settings`; every other former
  shim has been promoted to a method on
  :class:`SettingsLoader` / :class:`ManagedPolicySource` /
  :class:`UserConfigStore` / :class:`CloudModelMigrator`.
* The ``asyncio.run(...)`` calls in the mode-dispatch tail happen
  in :mod:`ember_code.cli.__init__` so ``tests/test_cli.py`` can
  patch ``ember_code.cli.asyncio.run``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from ember_code.cli.context import CliContext
from ember_code.cli.logging import DebugLogging
from ember_code.cli.options import CliOptions
from ember_code.cli.resume import ResumeResolver
from ember_code.core.config import settings as _settings_module
from ember_code.core.config.settings import Settings
from ember_code.core.worktree import WorktreeManager

_HELP_TEXT = (
    "igni no longer ships a terminal chat UI. Use one of:\n"
    '  • ember -m "<message>"        one-shot message\n'
    "  • ember -p                       pipe mode (stdin → stdout)\n"
    '  • ember -c -m "<message>"     resume last session, one-shot\n'
    "  • python -m ember_code.backend --socket <path>\n"
    "        start the backend for a React client (web / Tauri /\n"
    "        VSCode / JetBrains) to connect via Unix socket.\n"
)


class CliInvocation:
    """The single owner of everything the CLI callback does after
    Click has parsed argv and merged settings.

    Composition surface:

    * :class:`CliOptions` — typed view of the Click params.
    * :class:`Settings` — the merged, validated settings snapshot.
    * :class:`click.Context` — used for :meth:`click.echo` routing
      and ``ctx.obj`` stashing so subcommands can read the same
      settings / worktree state.

    All methods are I/O-side-effecting by design (the callback IS
    the CLI's imperative shell) — the class exists to give that
    imperative shell a single named owner rather than a 200-line
    procedural cascade.
    """

    def __init__(
        self,
        options: CliOptions,
        settings: Settings,
        ctx: click.Context,
    ) -> None:
        self._options = options
        self._settings = settings
        self._ctx = ctx
        self._resume_session_id: str | None = None
        self._project_dir: Path | None = None
        self._additional_dirs: list[Path] | None = None
        self._worktree_manager: WorktreeManager | None = None

    # ── Startup phases ──────────────────────────────────────────

    def enable_debug_logging(self) -> None:
        """Attach the ``--debug`` file handler when the flag is set."""
        if not self._options.debug:
            return
        log_path = DebugLogging.enable()
        click.echo(f"Debug logging enabled → {log_path}")

    def resolve_resume_id(self) -> None:
        """Fill :attr:`_resume_session_id` from ``--session-id`` or
        ``--continue`` (in that order — an explicit id wins)."""
        opts = self._options
        if opts.session_id:
            self._resume_session_id = opts.session_id
            return
        if not opts.continue_session:
            return

        resolver = ResumeResolver(self._settings)
        lookup = resolver.latest_id()
        if lookup.error is not None:
            click.echo("Could not look up last session.")
            return
        if lookup.session_id is None:
            click.echo("No previous sessions found.")
            return
        self._resume_session_id = lookup.session_id
        click.echo(f"Resuming last session: {lookup.session_id}")

    def setup_worktree(self) -> None:
        """Create the isolated git worktree when ``--worktree`` is
        set. Aborts the CLI with exit code 1 on failure so callers
        see a clear stderr instead of a stack trace."""
        if not self._options.worktree:
            return
        wm = WorktreeManager(Path.cwd())
        result = wm.create_result(session_id=self._resume_session_id)
        if not result.ok or result.info is None:
            click.echo(f"worktree failed: {result.message}", err=True)
            raise SystemExit(1)
        info = result.info
        self._project_dir = info.worktree_path
        self._worktree_manager = wm
        click.echo(f"Worktree: {info.worktree_path} (branch: {info.branch_name})")

    def resolve_additional_dirs(self) -> None:
        """Resolve each ``--add-dir`` value to an absolute path."""
        raw = self._options.add_dir
        if not raw:
            return
        self._additional_dirs = [Path(d).resolve() for d in raw]

    def store_context(self) -> None:
        """Stash the typed :class:`CliContext` on ``ctx.obj`` so
        subcommands can read the same settings / worktree state."""
        self._ctx.obj = CliContext(
            settings=self._settings,
            worktree_manager=self._worktree_manager,
            project_dir=self._project_dir,
            additional_dirs=self._additional_dirs,
        )

    # ── Public accessors used by the click surface ──────────────

    @property
    def resume_session_id(self) -> str | None:
        return self._resume_session_id

    @property
    def project_dir(self) -> Path | None:
        return self._project_dir

    @property
    def additional_dirs(self) -> list[Path] | None:
        return self._additional_dirs

    @property
    def worktree_manager(self) -> WorktreeManager | None:
        return self._worktree_manager

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def options(self) -> CliOptions:
        return self._options

    # ── Mode dispatch tail ──────────────────────────────────────

    def read_pipe_message(self) -> str:
        """Read stdin and combine with ``-m`` for pipe mode.

        Exits with code 1 when neither stdin nor ``-m`` supplied a
        message — matches the pre-refactor error path.
        """
        text = sys.stdin.read().strip()
        message = self._options.message
        if message:
            text = f"{message}\n\n{text}" if text else message
        if not text:
            click.echo("Error: no input provided via stdin or -m", err=True)
            raise SystemExit(1)
        return text

    def echo_help_pointer(self) -> None:
        """Print the "use a React client instead" pointer when
        ``ember`` is called with no message and no subcommand."""
        click.echo(_HELP_TEXT)

    def cleanup_worktree(self) -> None:
        """Delegate to :meth:`WorktreeManager.report_cleanup`. Safe
        no-op when no worktree was created."""
        if self._worktree_manager is None:
            return
        self._worktree_manager.report_cleanup(click.echo)


def load_settings_from_options(options: CliOptions) -> Settings:
    """Build a :class:`Settings` from a :class:`CliOptions` bundle.

    Goes through ``_settings_module.load_settings`` (the module
    attribute) rather than a direct import so
    ``tests/test_cli.py`` can patch
    ``ember_code.core.config.settings.load_settings`` and observe
    the mock through the CLI call site.
    """
    # Local import: :mod:`core.config.models` pulls in Agno, which
    # is heavy. Keeping it lazy shaves start-up cost off the
    # ``--help`` / ``--version`` short paths (they never construct
    # a Settings).
    from ember_code.core.config.models import CliOverrides

    overrides = CliOverrides.from_options(options)
    # ``SettingsLoader.merge_cli`` accepts either the typed
    # :class:`CliOverrides` bundle OR a raw dict (union type). We
    # emit the dict form here because ``tests/test_cli.py`` inspects
    # the payload via ``mock_load.call_args[1]["cli_overrides"]``
    # expecting a dict shape — routing through
    # ``CliOverrides.to_settings_payload`` (a method on the class,
    # NOT a free function) keeps the assertion shape stable while
    # still going through the typed encoder.
    payload = overrides.to_settings_payload()
    return _settings_module.load_settings(cli_overrides=payload)

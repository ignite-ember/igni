"""Interactive session loop — public entry point for the ``igni`` REPL.

Thin wrapper over :class:`InteractiveSessionLoop` (see
:mod:`ember_code.core.session.interactive_loop`). The actual
coordinator, prompt-handler chain, and lifecycle methods live
there so this module stays a single-symbol public API.

Kept as a module-level ``async`` function (not a method on the
loop class) so external callers can keep importing
``run_session_interactive`` from ``ember_code.core.session``
without churn.
"""

from pathlib import Path

from ember_code.core.config.settings import Settings
from ember_code.core.session.interactive_loop import InteractiveSessionLoop


async def run_session_interactive(
    settings: Settings,
    resume_session_id: str | None = None,
    project_dir: Path | None = None,
    additional_dirs: list[Path] | None = None,
) -> None:
    """Run an interactive REPL session.

    Public entry point preserved for backward compatibility;
    delegates to :class:`InteractiveSessionLoop.run`.
    """
    await InteractiveSessionLoop(
        settings,
        resume_session_id=resume_session_id,
        project_dir=project_dir,
        additional_dirs=additional_dirs,
    ).run()

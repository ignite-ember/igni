"""Single-message session runner — thin shim over
:class:`SingleMessageRun`.

Kept as a module-level ``async`` function so ``cli/__init__.py``'s
``_session_module.run_single_message`` attribute-patch pattern
keeps working. The pipeline lives in :class:`SingleMessageRun`,
which shares :class:`SessionRun` with :class:`InteractiveSessionLoop`
so the SessionStart / SessionEnd hook emit sites and the
``_run_turn`` pipeline exist in exactly one place.
"""

from pathlib import Path

from ember_code.core.config.settings import Settings
from ember_code.core.session.single_message_run import SingleMessageRun


async def run_single_message(
    settings: Settings,
    message: str,
    resume_session_id: str | None = None,
    project_dir: Path | None = None,
    additional_dirs: list[Path] | None = None,
) -> None:
    """Run a single non-interactive message.

    Public entry point preserved for backward compatibility;
    delegates to :meth:`SingleMessageRun.run`.
    """
    await SingleMessageRun(
        settings,
        resume_session_id=resume_session_id,
        project_dir=project_dir,
        additional_dirs=additional_dirs,
    ).run(message)

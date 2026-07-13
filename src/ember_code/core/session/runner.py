"""Single-message session runner."""

import time
from pathlib import Path

from ember_code.backend.command_handler import CommandHandler
from ember_code.core.config.settings import Settings
from ember_code.core.hooks.events import HookEvent
from ember_code.core.session.core import Session
from ember_code.core.utils.display import print_info, print_response, print_run_stats
from ember_code.core.utils.media import resolve_file_references
from ember_code.core.utils.mentions import process_file_mentions


async def run_single_message(
    settings: Settings,
    message: str,
    resume_session_id: str | None = None,
    project_dir: Path | None = None,
    additional_dirs: list[Path] | None = None,
):
    """Run a single non-interactive message."""

    session = Session(
        settings,
        project_dir=project_dir,
        resume_session_id=resume_session_id,
        additional_dirs=additional_dirs,
    )

    await session.hook_executor.execute(
        event=HookEvent.SESSION_START.value,
        payload={"session_id": session.session_id},
    )

    # Slash commands — handle without sending to LLM
    if message.startswith("/"):
        handler = CommandHandler(session)
        result = await handler.handle(message)
        if result.content:
            print_info(result.content)
        await session.hook_executor.execute(
            event=HookEvent.SESSION_END.value,
            payload={"session_id": session.session_id},
        )
        return

    # Process @file mentions — strip @ prefix and add read hint
    message, mentioned_files = process_file_mentions(message)
    if mentioned_files:
        print_info(f"Referenced: {', '.join(mentioned_files)}")

    # Resolve bare filenames to absolute paths
    message, resolved_files = resolve_file_references(message, project_dir=session.project_dir)
    if resolved_files:
        print_info(f"Resolved: {', '.join(resolved_files)}")

    start_time = time.monotonic()
    response = await session.handle_message(message)
    elapsed = time.monotonic() - start_time
    print_response(response)
    print_run_stats(
        elapsed_seconds=elapsed,
        model=session.settings.models.default,
    )

    await session.hook_executor.execute(
        event=HookEvent.SESSION_END.value,
        payload={"session_id": session.session_id},
    )

"""Abstract base class shared by the interactive REPL and single-shot runs.

Both :class:`InteractiveSessionLoop` (in
:mod:`ember_code.core.session.interactive_loop`) and
:class:`SingleMessageRun` (in
:mod:`ember_code.core.session.single_message_run`) subclass
:class:`SessionRun`. The base owns the state and machinery they used
to duplicate:

* :class:`Session` construction (previously done independently in
  ``runner.py`` and ``interactive_loop.py``).
* :meth:`_fire_session_start` / :meth:`_fire_session_end` — the two
  hook-emit sites, backed by
  :class:`SessionLifecyclePayload` (previously three raw
  ``{"session_id": ...}`` dict literals in ``runner.py`` and two
  typed calls in ``interactive_loop.py``).
* :meth:`_run_turn` — the ``@`` mention + :class:`MediaResolver` +
  ``session.handle_message`` + :class:`RunStats` pipeline that was
  byte-for-byte duplicated between the two coordinators.

Concrete subclasses define their own ``run(...)`` entry point shape
(``run()`` for the REPL, ``run(message)`` for the single-shot case).
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from pathlib import Path

from ember_code.core.config.settings import Settings
from ember_code.core.hooks.events import HookEvent
from ember_code.core.session.core import Session
from ember_code.core.session.schemas import SessionLifecyclePayload
from ember_code.core.utils.display_schemas import RunStats
from ember_code.core.utils.media import MediaResolver
from ember_code.core.utils.mentions import process_file_mentions


class SessionRun(ABC):
    """Abstract base holding the machinery shared between the
    interactive REPL loop and the single-shot CLI runner.

    Fields are prefixed with ``_`` because they are protected state
    intended for subclass access only. External callers reach the
    session (when they need it) via the concrete subclass's own
    public methods, not by touching :attr:`_session` directly.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        resume_session_id: str | None = None,
        project_dir: Path | None = None,
        additional_dirs: list[Path] | None = None,
    ) -> None:
        self._settings = settings
        self._resume_session_id = resume_session_id
        self._session = Session(
            settings,
            project_dir=project_dir,
            resume_session_id=resume_session_id,
            additional_dirs=additional_dirs,
        )

    # ── Hook emit sites (single source of truth) ──────────────────

    async def _fire_session_start(self) -> None:
        """Emit the ``SessionStart`` hook with a typed payload."""
        await self._session.hook_executor.execute(
            event=HookEvent.SESSION_START.value,
            payload=SessionLifecyclePayload(
                session_id=self._session.session_id,
                resumed=bool(self._resume_session_id),
            ).model_dump(),
        )

    async def _fire_session_end(self) -> None:
        """Emit the ``SessionEnd`` hook with a typed payload."""
        await self._session.hook_executor.execute(
            event=HookEvent.SESSION_END.value,
            payload=SessionLifecyclePayload(
                session_id=self._session.session_id,
                resumed=bool(self._resume_session_id),
            ).model_dump(),
        )

    # ── Shared turn pipeline ─────────────────────────────────────

    async def _run_turn(self, text: str) -> None:
        """Resolve file mentions / references, run the model, print
        the response + run stats.

        Single source of truth for the turn pipeline. Previously
        duplicated verbatim between ``runner.py`` and
        ``interactive_loop.py``.
        """
        session = self._session
        display = session.display

        message, mentioned_files = process_file_mentions(text)
        if mentioned_files:
            display.print_info(f"Referenced: {', '.join(mentioned_files)}")

        resolver = MediaResolver(project_dir=session.project_dir)
        message, resolved_files = resolver.resolve_text_references(message)
        if resolved_files:
            display.print_info(f"Resolved: {', '.join(resolved_files)}")

        start_time = time.monotonic()
        response = await session.handle_message(message)
        elapsed = time.monotonic() - start_time
        display.print_response(response)
        display.print_run_stats(
            RunStats(
                elapsed_seconds=elapsed,
                model=session.settings.models.default,
            )
        )

    # ── Entry point (subclass-defined shape) ─────────────────────

    @abstractmethod
    async def run(self, *args, **kwargs) -> None:
        """Subclass entry point. Shape differs between the REPL
        (``run()``) and the single-shot case (``run(message)``)."""

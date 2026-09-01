"""Non-interactive session runner — the OOP peer of
:class:`InteractiveSessionLoop`.

Both coordinators subclass :class:`SessionRun`, so the SessionStart
/ SessionEnd hook emit sites and the ``@`` mention + MediaResolver
+ ``handle_message`` + :class:`RunStats` pipeline live in one place
(``session_run.py``). This module adds only what's unique to the
single-shot case:

* :meth:`SingleMessageRun._dispatch_slash` — the slash-command
  early-return branch. Wraps :class:`CommandHandler` and prints any
  non-empty result. It stays a method (rather than a full
  :class:`PromptHandler` chain) because the non-interactive path
  has exactly two branches — slash vs message — so a chain would
  be over-engineered here.
* :meth:`SingleMessageRun.run` — the top-level orchestration:
  SessionStart hook, slash-dispatch (early return), else
  :meth:`SessionRun._run_turn`, SessionEnd hook.

.. note::

    ``runner.py`` keeps a module-level ``async def
    run_single_message`` shim so the CLI's
    ``_session_module.run_single_message`` pattern (and any
    ``patch("ember_code.core.session.runner.run_single_message")``
    test target) continues to work.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from ember_code.backend.command_handler import CommandHandler
from ember_code.core.session.session_run import SessionRun

logger = logging.getLogger(__name__)


class SingleMessageRun(SessionRun):
    """Runs one message end-to-end and exits.

    Subclasses :class:`SessionRun` so the base owns
    :class:`Session` construction, the SessionStart / SessionEnd
    hook emit sites, and the shared ``_run_turn`` pipeline. This
    class contributes only what's unique to the non-interactive
    path.
    """

    # ── Slash-command dispatch ────────────────────────────────────
    #
    # TODO: the four :class:`PromptHandler` subclasses in
    # ``interactive_loop.py`` are REPL-coupled (``_QuitHandler``
    # raises a private ``_LoopExit`` sentinel, ``_SkillHandler``
    # uses ``print_info``, ``_MessageHandler`` calls the loop's
    # ``_run_turn``). A future refactor could extract a headless
    # ``SlashCommandRouter`` collaborator both coordinators share,
    # but that's out of scope for this diff — the non-interactive
    # path only has two branches, so a full chain here would be
    # over-engineered.

    async def _dispatch_slash(self, message: str) -> bool:
        """Try to consume ``message`` as a slash command.

        Returns ``True`` when the message started with ``/`` and
        was handled by :class:`CommandHandler` (caller should
        early-return to the SessionEnd hook without calling the
        model); ``False`` otherwise.
        """
        if not message.startswith("/"):
            return False
        handler = CommandHandler(self._session)
        result = await handler.handle(message)
        if result.content:
            self._session.display.print_info(result.content)
        return True

    # ── Public entry point ────────────────────────────────────────

    async def run(self, message: str) -> None:  # type: ignore[override]
        """Run one message: SessionStart → dispatch → SessionEnd.

        Slash-prefixed messages take the ``_dispatch_slash`` branch
        and intentionally skip ``_run_turn`` — they never reach the
        model. Non-slash messages flow through the shared
        :meth:`SessionRun._run_turn` pipeline. SessionEnd always
        fires exactly once.
        """
        await self._fire_session_start()

        if await self._dispatch_slash(message):
            # Slash branch: no model call. Jump straight to SessionEnd.
            await self._fire_session_end()
            return

        runtime = await self._attach_neo4j()
        try:
            await self._run_turn(message)
        finally:
            await self._release_neo4j(runtime)
        await self._fire_session_end()

    # ── Neo4j-backed features ─────────────────────────────────────

    async def _attach_neo4j(self) -> Any:
        """Give this session a Neo4j runtime, so CodeIndex works here.

        ``Neo4jRuntime`` was constructed in exactly one place —
        ``backend/session_orchestrator.py`` — so in non-interactive mode the
        CodeIndex toolkit was attached with no backend behind it and every
        query came back ``{"error": "no_backend"}``. The agent would then fall
        back to shell, which reads as a working session and is not one.

        Gating mirrors the orchestrator's, including the reason the code_index
        check is on the availability flag rather than the attribute: a disabled
        code_index deliberately keeps its objects, so testing the attribute
        would wire Neo4j for a feature an admin turned off.

        Returns the runtime (or ``None``), for :meth:`_release_neo4j`.
        """
        if os.environ.get("EMBER_NEO4J_DISABLED"):
            logger.info("EMBER_NEO4J_DISABLED set — skipping the Neo4j runtime attach.")
            return None

        session = self._session
        settings = session.settings
        wants_codeindex = bool(getattr(getattr(settings, "code_index", None), "enabled", False))
        wants_knowledge = bool(getattr(getattr(settings, "knowledge", None), "enabled", False))
        if not (wants_codeindex or wants_knowledge):
            return None

        try:
            from ember_code.backend.neo4j_runtime import Neo4jRuntime

            runtime = Neo4jRuntime(data_dir=settings.storage.data_dir)
        except Exception:  # noqa: BLE001 — degrade to no-index, don't kill the run
            logger.exception(
                "Neo4j runtime construction failed; CodeIndex is unavailable this "
                "session. Set EMBER_NEO4J_DISABLED=1 to skip Neo4j entirely."
            )
            return None

        try:
            if wants_knowledge and getattr(session, "knowledge", None) is not None:
                await session.attach_knowledge_neo4j(runtime)
            if wants_codeindex and getattr(session, "code_index", None) is not None:
                await session.attach_codeindex_neo4j(runtime)
        except Exception:  # noqa: BLE001
            logger.exception("Attaching the Neo4j runtime failed; continuing without it")
            await self._release_neo4j(runtime)
            return None

        await self._note_head_commit()
        return runtime

    async def _note_head_commit(self) -> None:
        """Record which ``(project, commit)`` this run will acquire.

        Deliberately does *not* call ``start_for_commit``. ``CodeIndex._client_for``
        already does that on first query and caches the client per sha, so it
        acquires exactly once per session — an explicit start here made two
        acquires against one release, and ``stop_for_commit`` then logged "still
        attached by 1 other BE(s)" and left the server running after every run.

        An earlier version did start it eagerly, which was necessary only while
        the toolkit held a runtime-less index and so never reached
        ``_client_for`` at all. With the index provider wired, the lazy acquire
        happens on demand and this only has to remember what to give back.
        """
        session = self._session
        index = getattr(session, "code_index", None)
        sync = getattr(session, "code_index_sync", None)
        if index is None or sync is None:
            return
        try:
            head = sync.current_sha()
            if not head or not index.has_commit(head):
                # Nothing indexed for this commit; no acquire will happen.
                return
            self._started_commit = (index.project_id, head)
        except Exception:  # noqa: BLE001 — no index is a degraded run, not a failed one
            logger.exception("Could not resolve HEAD for the code index")

    async def _release_neo4j(self, runtime: Any) -> None:
        """Give back the one ``(project, commit)`` this run acquired.

        Deliberately ``stop_for_commit`` and not ``stop_all``: the latter
        ignores refcounts and kills every active pair, which is right at
        backend shutdown and wrong here — a backend or a second CLI attached to
        the same pair would lose its server. ``stop_for_commit`` decrements and
        only shuts down at zero.

        Something has to release it, though: nothing in this mode has ever
        called the backend's shutdown pipeline, so the refcount would climb by
        one per invocation and the sidecar would outlive every CLI that started
        it.
        """
        if runtime is None:
            return
        if os.environ.get("EMBER_NEO4J_KEEP_ALIVE"):
            # Opt-in for batch callers that run many single-shot sessions over
            # the same repo. Releasing at zero shuts the server down, and the
            # next session pays a cold start: measured 44s end-to-end cold
            # against 5-8s attaching to a warm one. Over a few thousand
            # sessions that is the difference between hours and days, so the
            # caller can choose to keep the pair up and reclaim it once at the
            # end (``pkill -f CommunityEntryPoint``, or a backend shutdown).
            logger.info("EMBER_NEO4J_KEEP_ALIVE set — leaving the Neo4j pair running.")
            return
        started = getattr(self, "_started_commit", None)
        try:
            if started is not None:
                await runtime.stop_for_commit(*started)
        except Exception:  # noqa: BLE001 — teardown must not fail the run
            logger.debug("Neo4j runtime release failed", exc_info=True)

"""Interactive REPL coordinator — the OOP core of the ``igni`` prompt loop.

Split from :mod:`ember_code.core.session.interactive` so that module
stays a thin public entry point (``run_session_interactive``). This
module owns the class hierarchy that used to be a 152-line procedural
coroutine:

* :class:`InteractiveSessionLoop` — coordinator; holds the
  :class:`Session`, the pre-built
  :class:`InteractiveCommandDispatcher`, and the ordered
  :class:`PromptHandler` chain. Each of the seven audit-flagged
  concerns (startup, shutdown, prompt read, knowledge in / out,
  banner, update check, turn) is a named method rather than an
  inline stanza.
* :class:`PromptHandler` — abstract base for the input-dispatch
  chain. Subclasses implement :meth:`handle` and return ``True``
  when they consumed the line. Replaces the five-way
  ``if/continue`` fall-through with polymorphism, mirroring
  ``commands.py``'s ``_ACTION_HANDLERS`` dispatch style.
* :class:`_QuitHandler` / :class:`_SlashCommandHandler` /
  :class:`_SkillHandler` / :class:`_MessageHandler` — the four
  concrete handlers, evaluated in that order. ``_MessageHandler``
  is the fall-through and must stay last.

The private :class:`_LoopExit` sentinel is the ``_QuitHandler``'s
break signal; the outer :meth:`InteractiveSessionLoop._loop`
catches it to end the REPL cleanly. Slash-form quits (``/quit``,
``/exit``) continue to reach
:meth:`InteractiveCommandDispatcher._render_quit` and preserve
its ``SystemExit(0)`` semantics — only bare ``quit`` / ``exit``
words go through the sentinel path.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path

from rich.prompt import Prompt

from ember_code import __version__
from ember_code.core.config.settings import Settings
from ember_code.core.session.commands import InteractiveCommandDispatcher
from ember_code.core.session.schemas import InteractiveBanner
from ember_code.core.session.session_run import SessionRun
from ember_code.core.skills.executor import SkillExecutor
from ember_code.core.utils.audit import AuditEntry
from ember_code.core.utils.tips import get_tip
from ember_code.core.utils.update_checker import check_for_update

logger = logging.getLogger(__name__)


class _LoopExit(Exception):
    """Private sentinel raised by :class:`_QuitHandler` to break the
    outer REPL loop cleanly. Not exported — only
    :meth:`InteractiveSessionLoop._loop` catches it."""


# ── Prompt-handler dispatch chain ─────────────────────────────────


class PromptHandler(ABC):
    """Abstract base for a link in the input-dispatch chain.

    Subclasses implement :meth:`handle` and return ``True`` when
    they consumed the input line (subsequent handlers are
    skipped) or ``False`` to defer to the next handler. The
    parent :class:`InteractiveSessionLoop` is passed in so
    subclasses can reach ``self._loop._session`` /
    ``self._loop._dispatcher`` via composition rather than
    inheritance.
    """

    def __init__(self, loop: InteractiveSessionLoop) -> None:
        self._loop = loop

    @abstractmethod
    async def handle(self, text: str) -> bool:
        """Try to consume ``text``. Return ``True`` when handled."""


class _QuitHandler(PromptHandler):
    """Handles the bare ``quit`` / ``exit`` words (case-insensitive).

    Slash-prefixed ``/quit`` / ``/exit`` fall through to
    :class:`_SlashCommandHandler`, which routes them to
    :meth:`InteractiveCommandDispatcher._render_quit` — that path
    raises :class:`SystemExit` because slash-commands are the
    explicit REPL exit contract. The bare words are a REPL-only
    convenience and use :class:`_LoopExit` for a clean loop-break.
    """

    _QUIT_WORDS = frozenset({"quit", "exit"})

    async def handle(self, text: str) -> bool:
        if text.lower() not in self._QUIT_WORDS:
            return False
        self._loop._session.display.print_info("Goodbye!")
        raise _LoopExit()


class _SlashCommandHandler(PromptHandler):
    """Delegates to :class:`InteractiveCommandDispatcher` for any
    slash-prefixed input.

    The dispatcher itself decides whether the command was
    understood: if it returns ``False``, this handler also
    returns ``False`` so the next handler (skill match) gets a
    chance.
    """

    async def handle(self, text: str) -> bool:
        if not text.startswith("/"):
            return False
        return await self._loop._dispatcher.dispatch(text)


class _SkillHandler(PromptHandler):
    """Matches ``/skill-name args`` against the loaded skill pool
    and, on hit, runs the skill via :class:`SkillExecutor`.

    Logs the execution to the audit log so slash-skill runs
    appear alongside tool calls.

    The :meth:`SkillExecutor.execute` call is wrapped in a broad
    ``try/except`` because :class:`SkillExecutor` only catches
    expected agent-runtime failures — programming bugs
    (``AttributeError``, ``TypeError``, …) intentionally
    propagate so they're not silently flattened to a user-facing
    "Error: ..." string. Catching here keeps a buggy skill from
    killing the REPL; the failure still surfaces to the user via
    ``print_error`` and to the audit log.
    """

    async def handle(self, text: str) -> bool:
        session = self._loop._session
        skill_match = session.skill_pool.match_user_command(text)
        if not skill_match:
            return False
        skill, args = skill_match
        session.display.print_info(f"Running skill: /{skill.name}")
        try:
            result = await SkillExecutor(
                session.pool, session.settings, session.session_id
            ).execute(skill, args)
        except Exception as e:
            session.display.print_error(f"Skill '{skill.name}' crashed: {e}")
            session.audit.log(
                AuditEntry.error(
                    session_id=session.session_id,
                    agent_name="skill",
                    tool_name=f"/{skill.name}",
                    error=str(e),
                    error_type=type(e).__name__,
                )
            )
            return True
        session.display.print_response(result.text)
        session.audit.log(
            AuditEntry.tool_call(
                session_id=session.session_id,
                agent_name="skill",
                tool_name=f"/{skill.name}",
                args=args,
            )
        )
        return True


class _MessageHandler(PromptHandler):
    """Fall-through handler — runs the model turn.

    Always returns ``True`` so it terminates the dispatch chain,
    which means it MUST be the last entry in
    :attr:`InteractiveSessionLoop._handlers`.
    """

    async def handle(self, text: str) -> bool:
        await self._loop._run_turn(text)
        return True


# ── Coordinator ────────────────────────────────────────────────────


class InteractiveSessionLoop(SessionRun):
    """The interactive REPL, encapsulated as a class.

    Subclasses :class:`SessionRun` so the SessionStart / SessionEnd
    hook emit sites and the ``_run_turn`` pipeline are inherited
    (previously duplicated verbatim with ``runner.py``).

    Replaces the ex-``run_session_interactive`` coroutine's
    seven-concern monolith with named async methods
    (:meth:`_startup`, :meth:`_shutdown`, :meth:`_read_prompt`,
    :meth:`_sync_knowledge_in`, :meth:`_sync_knowledge_out`,
    :meth:`_print_banner`, :meth:`_check_update`) so each stanza
    has one job and the outer :meth:`run` reads top-to-bottom.

    The :class:`InteractiveCommandDispatcher` is built once in
    ``__init__`` (not per-line) and stored as :attr:`_dispatcher`.
    The dispatcher must remain stateless apart from
    ``self.session`` — a future contributor sneaking per-turn
    state onto it would silently change lifecycle semantics.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        resume_session_id: str | None = None,
        project_dir: Path | None = None,
        additional_dirs: list[Path] | None = None,
    ) -> None:
        # super().__init__ must run first: it builds ``self._session``,
        # which the dispatcher and handler chain below reach into.
        super().__init__(
            settings,
            resume_session_id=resume_session_id,
            project_dir=project_dir,
            additional_dirs=additional_dirs,
        )
        # Built once — the dispatcher is stateless apart from
        # ``self.session`` today, and this invariant must hold
        # (per-turn state on the dispatcher would silently change
        # lifecycle semantics now that it survives across turns).
        self._dispatcher = InteractiveCommandDispatcher(self._session)
        # Ordered dispatch chain — first hit wins. ``_MessageHandler``
        # is the fall-through and must stay last.
        self._handlers: list[PromptHandler] = [
            _QuitHandler(self),
            _SlashCommandHandler(self),
            _SkillHandler(self),
            _MessageHandler(self),
        ]

    # ── Public entry point ────────────────────────────────────────

    async def run(self) -> None:
        """Drive the REPL: startup, loop, shutdown (guaranteed)."""
        await self._startup()
        try:
            await self._loop()
        finally:
            await self._shutdown()

    # ── Lifecycle ─────────────────────────────────────────────────

    async def _startup(self) -> None:
        """Emit SessionStart, load shared knowledge, print banner."""
        await self._fire_session_start()
        await self._sync_knowledge_in()
        await self._print_banner()

    async def _shutdown(self) -> None:
        """Cleanup ephemeral agents, export knowledge, emit SessionEnd."""
        session = self._session
        if session.settings.orchestration.auto_cleanup:
            removed = session.pool.cleanup_ephemeral()
            if removed:
                session.display.print_info(f"Cleaned up {removed} ephemeral agent(s).")
        await self._sync_knowledge_out()
        await self._fire_session_end()

    # ── REPL body ─────────────────────────────────────────────────

    async def _loop(self) -> None:
        """The prompt-read / dispatch loop.

        Reads a line, skips blanks, then hands the trimmed text
        to each :class:`PromptHandler` in order. The first
        handler that returns ``True`` wins. Terminates on
        :class:`_LoopExit` (bare quit words), Ctrl+C, or EOF.
        """
        display = self._session.display
        while True:
            try:
                message = self._read_prompt()
            except KeyboardInterrupt:
                display.print_info("\nGoodbye!")
                return
            except EOFError:
                return

            text = message.strip()
            if not text:
                continue

            try:
                for handler in self._handlers:
                    if await handler.handle(text):
                        break
            except _LoopExit:
                return
            except KeyboardInterrupt:
                display.print_info("\nGoodbye!")
                return
            except EOFError:
                return

    def _read_prompt(self) -> str:
        """Read one line from the user. Isolated so the loop shell
        is a single tidy line."""
        return Prompt.ask("\n[bold blue]>[/bold blue]")

    # ── Knowledge sync (symmetric pair) ───────────────────────────

    @property
    def _knowledge_sync_enabled(self) -> bool:
        """Shared guard for the two sync directions."""
        knowledge = self._session.settings.knowledge
        return knowledge.share and knowledge.auto_sync

    async def _sync_knowledge_in(self) -> None:
        """Pull shared knowledge from disk into the DB at startup."""
        if not self._knowledge_sync_enabled:
            return
        session = self._session
        sync_result = await session.knowledge_mgr.sync_from_file()
        if sync_result.new_entries > 0:
            session.display.print_info(
                f"Knowledge sync: loaded {sync_result.new_entries} new entries from git"
            )
        elif sync_result.error:
            session.display.print_error(f"Knowledge sync error: {sync_result.error}")

    async def _sync_knowledge_out(self) -> None:
        """Push new DB entries back out to the shared file at shutdown."""
        if not self._knowledge_sync_enabled:
            return
        session = self._session
        sync_result = await session.knowledge_mgr.sync_to_file()
        if sync_result.new_entries > 0:
            session.display.print_info(
                f"Knowledge sync: exported {sync_result.new_entries} "
                f"new entries to {session.settings.knowledge.share_file}"
            )

    # ── Banner + update check ─────────────────────────────────────

    async def _check_update(self) -> str | None:
        """Return the update warning message when one exists.

        ``check_for_update`` is documented to never raise —
        it returns an :class:`UpdateInfo` with an ``error``
        field on failure — so we inspect that field explicitly
        instead of wrapping the call in a ``try/except: pass``.
        """
        update_info = await check_for_update()
        if update_info.error:
            logger.debug("Update check failed: %s", update_info.error)
            return None
        if update_info.available:
            return update_info.message
        return None

    async def _print_banner(self) -> None:
        """Build the :class:`InteractiveBanner` view-model once and
        render it. Replaces five ad-hoc ``print_info`` calls."""
        session = self._session
        banner = InteractiveBanner(
            version=__version__,
            tip=get_tip(session.settings, session.project_dir),
            update_message=await self._check_update(),
            agent_names=list(session.pool.agent_names),
            skill_names=[s.name for s in session.skill_pool.list_skills()],
            hook_count=sum(len(v) for v in session.hooks_map.values()),
            session_id=session.session_id,
            resumed=bool(self._resume_session_id),
        )
        banner.render(session.display)

    # ── Model turn ────────────────────────────────────────────────
    #
    # Inherited from :class:`SessionRun`: ``_run_turn`` is defined
    # once on the base and shared with :class:`SingleMessageRun`.

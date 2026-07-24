"""Permission-mode slash commands: ``/plan``, ``/accept``, ``/bypass``.

Extracted from :mod:`ember_code.backend.command_handler` — three
commands that share the same shape (toggle the session's live
`PermissionEvaluator.mode` without rebuilding the agent) but each
flip into a different mode:

* ``/plan`` — enter/exit ``PermissionMode.PLAN``. File edits +
  mutating shell blocked; agent should call ``exit_plan_mode``
  when ready.
* ``/accept`` — enter/exit ``PermissionMode.ACCEPT_EDITS``. File
  edits auto-approve; shell still asks. For trusted-loop
  workflows.
* ``/bypass`` — enter/exit ``PermissionMode.BYPASS_PERMISSIONS``.
  ANY tool auto-approves. For fully-autonomous loops.

Bypass-resistant scoped denies (from row 9 — e.g.
``deny: save_file(.env)``) still hold in every mode. Only the
per-tool ``ask`` step is skipped; deny always wins.

All three commands accept the same arg vocabulary:

* (empty) / ``toggle`` — flip target ↔ default.
* ``on`` / ``enable`` / ``start`` — enter target.
* ``off`` / ``disable`` / ``exit`` / ``stop`` — return to
  default.
* ``status`` / ``show`` — report the current mode without
  changing it.

OOP surface: :class:`ModeToggle` is a Pydantic base carrying the
declarative shape of one ``/mode`` command (target mode, tail
messages) with :meth:`ModeToggle.enter` / :meth:`ModeToggle.exit`
hooks defaulting to no-ops. :class:`PlanModeToggle`,
:class:`AcceptModeToggle`, and :class:`BypassModeToggle` are the
three subclasses — only :class:`PlanModeToggle` overrides
``enter`` / ``exit`` to arm/disarm the researcher-nudge. The
three toggles live as ``ClassVar`` singletons on
:class:`ModesCommand`, which owns the shared arg-parse +
transition mechanics and exposes the three verb methods
(``plan`` / ``accept`` / ``bypass``) plus classmethod command
entry points bound at the module scope for the registry import
contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel, ConfigDict

from ember_code.backend.command_result import CommandResult
from ember_code.core.config.permission_eval import PermissionMode
from ember_code.protocol.messages import CommandResultKind

if TYPE_CHECKING:
    from ember_code.backend.command_handler import CommandHandler
    from ember_code.core.session import Session


_ON_TOKENS = frozenset({"on", "enable", "start"})
_OFF_TOKENS = frozenset({"off", "disable", "exit", "stop"})
_TOGGLE_TOKENS = frozenset({"", "toggle"})
_STATUS_TOKENS = frozenset({"status", "show"})


class ModeToggle(BaseModel):
    """Declarative shape of one ``/mode`` command.

    Everything that differs between ``/plan``, ``/accept``, and
    ``/bypass`` lives here — :class:`ModesCommand` stays generic
    and drives instances of this class polymorphically.

    :meth:`enter` fires when the mode flips FROM something-else
    INTO :attr:`target`; :meth:`exit` fires on the return leg.
    Both default to no-ops on the base — only
    :class:`PlanModeToggle` overrides them (to arm/disarm the
    researcher-nudge for the next turn).

    Subclasses pin fields via defaults so ``PlanModeToggle()``
    etc. construct without args and can be stored as
    ``ClassVar`` singletons on :class:`ModesCommand`.
    """

    model_config = ConfigDict(frozen=True)

    command_name: str
    target: PermissionMode
    enter_tail: str
    exit_tail: str

    def enter(self, session: Session) -> None:
        """Hook fired when the session flips into :attr:`target`.

        Base impl is a no-op — subclasses override when a
        transition-in side effect is required. The coordinator
        (:meth:`ModesCommand._run`) owns the *should we fire*
        guard (only on an actual mode change); this method
        answers only *what happens when we do*.
        """

    def exit(self, session: Session) -> None:
        """Hook fired when the session flips OUT of :attr:`target`.

        Base impl is a no-op — subclasses override to reverse
        an :meth:`enter` side effect.
        """

    def target_for(
        self, normalized: str, current: PermissionMode
    ) -> tuple[PermissionMode | None, str]:
        """Map the user's arg to a target mode.

        Returns ``(target, error_msg)`` — exactly one of the two is
        non-empty/non-None. A ``None`` target with an empty error
        means "status only, don't flip".

        Lives on the toggle (not on :class:`ModesCommand`) so
        subclasses could refine it if a mode ever needed
        mode-specific verbs — today the vocabulary is uniform, so
        the base impl covers all three.
        """
        if normalized in _STATUS_TOKENS:
            return None, ""
        if normalized in _ON_TOKENS:
            return self.target, ""
        if normalized in _OFF_TOKENS:
            return PermissionMode.DEFAULT, ""
        if normalized in _TOGGLE_TOKENS:
            return (PermissionMode.DEFAULT if current is self.target else self.target), ""
        return None, (
            f"Unknown {self.command_name} argument: {normalized!r}. "
            f"Use {self.command_name}, {self.command_name} on, "
            f"{self.command_name} off, or {self.command_name} status."
        )


class PlanModeToggle(ModeToggle):
    """``/plan`` — enter/exit :attr:`PermissionMode.PLAN`.

    The only subclass that overrides :meth:`enter` / :meth:`exit`:
    arms/disarms the plan-mode researcher-nudge on the
    :class:`Session` so the next turn kicks off a research pass
    when the user enters plan mode.
    """

    command_name: str = "/plan"
    target: PermissionMode = PermissionMode.PLAN
    enter_tail: str = (
        "\n\nYou are now in **plan mode**. The agent can read, "
        "search, and think but file edits and mutating shell "
        "commands are blocked. The agent should call "
        "`exit_plan_mode(plan)` when ready; use `/plan` again "
        "to exit plan mode and let it execute."
    )
    exit_tail: str = "\n\nPlan mode exited. The agent can now execute as normal."

    def enter(self, session: Session) -> None:
        session.set_plan_research_armed(True)

    def exit(self, session: Session) -> None:
        # ``set_plan_research_armed`` is a Session method — no
        # attribute reach-in needed, and Session owns the semantics.
        session.set_plan_research_armed(False)


class AcceptModeToggle(ModeToggle):
    """``/accept`` — enter/exit :attr:`PermissionMode.ACCEPT_EDITS`."""

    command_name: str = "/accept"
    target: PermissionMode = PermissionMode.ACCEPT_EDITS
    enter_tail: str = (
        "\n\nYou are now in **acceptEdits mode**. File-edit "
        "tools (`edit_file`, `save_file`, `create_file`) "
        "auto-approve without per-tool HITL prompts — useful "
        "for autonomous loops on trusted work. Scoped denies "
        "still block specific files (e.g. `.env`); use "
        "`/accept off` to leave the mode."
    )
    exit_tail: str = "\n\nacceptEdits exited. Edits now require approval per the default policy."


class BypassModeToggle(ModeToggle):
    """``/bypass`` — enter/exit :attr:`PermissionMode.BYPASS_PERMISSIONS`."""

    command_name: str = "/bypass"
    target: PermissionMode = PermissionMode.BYPASS_PERMISSIONS
    enter_tail: str = (
        "\n\nYou are now in **bypassPermissions mode**. The "
        "agent will run any tool without asking — useful for "
        "autonomous loops on trusted work. Scoped denies still "
        "block specific files (e.g. `.env`); use `/bypass off` "
        "to leave the mode."
    )
    exit_tail: str = (
        "\n\nbypassPermissions exited. Tools now require approval per the default policy."
    )


class ModesCommand:
    """Coordinator for the three permission-mode toggles.

    One instance per invocation. The three public methods
    (:meth:`plan`, :meth:`accept`, :meth:`bypass`) map to the
    three slash commands; each delegates to :meth:`_run` with
    the appropriate :class:`ModeToggle` singleton attached as a
    class-level constant (``PLAN`` / ``ACCEPT`` / ``BYPASS``) —
    no module-level state.
    """

    PLAN: ClassVar[ModeToggle] = PlanModeToggle()
    ACCEPT: ClassVar[ModeToggle] = AcceptModeToggle()
    BYPASS: ClassVar[ModeToggle] = BypassModeToggle()

    def __init__(self, session: Session) -> None:
        self._session = session

    async def plan(self, args: str) -> CommandResult:
        """Toggle the session in/out of plan mode (CC parity, row 50).

        The mode flip mutates the live
        ``Session.permission_evaluator`` so it takes effect on the
        very next tool call — no agent rebuild, no restart.
        """
        return await self._run(args, type(self).PLAN)

    async def accept(self, args: str) -> CommandResult:
        """Toggle the session in/out of acceptEdits mode (CC parity,
        row 51).

        Unlike plan mode, ``acceptEdits`` is LOOSENING the sandbox —
        the agent intentionally does NOT get a tool to flip itself
        in. Only the user can opt in via this slash command.
        Bypass-resistant scoped denies (row 9) still hold — even in
        acceptEdits, a ``deny`` rule like ``save_file(.env)`` still
        blocks.
        """
        return await self._run(args, type(self).ACCEPT)

    async def bypass(self, args: str) -> CommandResult:
        """Toggle the session in/out of bypassPermissions mode.

        In ``bypassPermissions`` mode the permission evaluator skips
        the HITL prompt for ANY tool — shell, edits, MCP — letting
        the agent run autonomously without per-tool approval. Used
        from the footer "Auto-approve" switch for trusted loops.
        Scoped denies (row 9) still hold — only the per-tool
        ``ask`` step is skipped.
        """
        return await self._run(args, type(self).BYPASS)

    # ── Shared toggle mechanics ─────────────────────────────────

    async def _run(self, args: str, toggle: ModeToggle) -> CommandResult:
        """Shared body of :meth:`plan`, :meth:`accept`, :meth:`bypass`."""
        normalized = args.strip().lower()
        current = self._session.permission_evaluator.mode

        if normalized in _STATUS_TOKENS:
            return CommandResult(
                kind=CommandResultKind.INFO,
                content=f"Permission mode: **{current.value}**",
            )

        target, error = toggle.target_for(normalized, current)
        if error:
            return CommandResult.error(error)
        assert target is not None  # status branch returned above

        status_line = self._session.set_permission_mode(target.value)

        if target is toggle.target:
            # Entering target mode — fire the transition-in hook,
            # but only on an actual mode change so a re-issue of
            # the same command doesn't retrigger side effects.
            if current is not toggle.target:
                toggle.enter(self._session)
            tail = toggle.enter_tail
        else:
            # Mirror the enter-guard: exit hook fires only on an
            # actual transition away from the target mode.
            if current is toggle.target:
                toggle.exit(self._session)
            tail = toggle.exit_tail

        return CommandResult(
            kind=CommandResultKind.MARKDOWN,
            content=status_line + tail,
        )

    # ── Classmethod command entry points ────────────────────────

    @classmethod
    async def plan_command(cls, handler: CommandHandler, args: str) -> CommandResult:
        """See :meth:`plan`."""
        return await cls(handler.session).plan(args)

    @classmethod
    async def accept_command(cls, handler: CommandHandler, args: str) -> CommandResult:
        """See :meth:`accept`."""
        return await cls(handler.session).accept(args)

    @classmethod
    async def bypass_command(cls, handler: CommandHandler, args: str) -> CommandResult:
        """See :meth:`bypass`."""
        return await cls(handler.session).bypass(args)


# Module-level bindings for the builtin-command-registry import
# contract. Python's classmethod descriptor auto-binds ``cls``, so
# each binding is already ``Callable[[CommandHandler, str],
# Awaitable[CommandResult]]`` — matches ``BuiltinHandler`` without
# a wrapper.
cmd_plan = ModesCommand.plan_command
cmd_accept = ModesCommand.accept_command
cmd_bypass = ModesCommand.bypass_command

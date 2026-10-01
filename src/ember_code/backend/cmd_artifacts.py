"""``/artifacts`` slash command implementations.

Sibling of :mod:`cmd_memory` — the two commands share the
"artifact store surface" concept but do not share the memory /
Learning Machine surface, so they live in their own module.

Sub-commands handled here:

* ``/artifacts`` (no args) — open the TUI panel via
  ``CommandResult.for_action(CommandAction.ARTIFACTS)``, or emit
  an error card when the base failed to initialize.
* ``/artifacts add <url|path|text>`` — triage the argument into a
  URL / path / plain-text add.
* ``/artifacts search`` — open the panel; the panel's input
  field IS the search UI, so any positional argument is ignored
  by design (documented on :meth:`ArtifactsCommand.search`).
Architecture mirrors :mod:`cmd_codeindex` / :mod:`cmd_memory` —
:class:`ArtifactsCommand` is Session-injected, holds no
cross-invocation state, and every verb is a bound method. The
public shim (:func:`cmd_artifacts`) is a two-liner that constructs
the coordinator and delegates to the right method so
:mod:`ember_code.backend.command_handler`'s dispatch table stays
wire-compatible.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ember_code.backend.command_result import CommandResult
from ember_code.backend.subsystem_status import ARTIFACTS, SubsystemState
from ember_code.protocol.messages import CommandAction

if TYPE_CHECKING:
    from ember_code.backend.command_handler import CommandHandler
    from ember_code.core.session import Session

logger = logging.getLogger(__name__)


class ArtifactsCommand:
    """Coordinator for the ``/artifacts``
    slash-command families.

    Session-injected — no ``handler._session`` reach-in from
    inside the class. Constructed per invocation so it stays
    stateless between calls (matches the sibling
    :class:`~ember_code.backend.cmd_codeindex.CodeIndexCommand`
    contract).
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    # ── Dispatch ─────────────────────────────────────────────────

    async def dispatch(self, args: str) -> CommandResult:
        """Route a raw arg string to the matching verb method.

        Recognised verbs:

        * ``add <url|path|text>`` → :meth:`add`
        * ``search`` (any positional arg ignored) → :meth:`search`
        * no arg / anything else → :meth:`panel`
        """
        parts = args.strip().split(None, 1)
        subcommand = parts[0].lower() if parts else ""
        sub_args = parts[1].strip() if len(parts) > 1 else ""

        match subcommand:
            case "add" if sub_args:
                return await self.add(sub_args)
            case "search":
                return self.search()
            case _:
                return await self.panel()

    # ── Verb methods ─────────────────────────────────────────────

    async def add(self, sub_args: str) -> CommandResult:
        """Add a URL / path / text entry to the artifact store.

        Triage is by shape:

        * starts with ``http://`` / ``https://`` → URL entry
        * contains a slash or leads with ``.`` → path entry
        * otherwise → plain-text entry
        """
        mgr = self._session.artifact_mgr
        if sub_args.startswith(("http://", "https://")):
            result = await mgr.add_url(sub_args)
        elif "/" in sub_args or sub_args.startswith("."):
            result = await mgr.add_path(sub_args)
        else:
            # Typed at a prompt, so there is nothing to declare and
            # nobody to ask — the reason is recorded instead of a
            # source, which is exactly what the empty case is for.
            first_line = next(
                (ln.strip().lstrip("# ").strip() for ln in sub_args.splitlines() if ln.strip()),
                "",
            )
            result = await mgr.create(
                name=first_line[:80] or "untitled note",
                content=sub_args,
                sources=[],
                no_sources_reason="typed directly into /artifacts add",
            )
        if not result.success:
            return CommandResult.error(result.error)
        return CommandResult.info(result.message)

    def search(self) -> CommandResult:
        """Open the TUI knowledge panel.

        The panel's input field IS the search UI — pre-populating
        it from the slash-command line would defeat the point of
        the panel (interactive typing, iterating, browsing).
        Any positional argument after ``search`` is intentionally
        ignored; users continue typing inside the panel.
        """
        return CommandResult.for_action(CommandAction.ARTIFACTS)

    async def panel(self) -> CommandResult:
        """Default no-arg entry: open the panel, or say why it cannot.

        The four answers come from the session's subsystem registry
        rather than from this method guessing between them — see
        :mod:`ember_code.backend.subsystem_status`. Guessing is what
        produced "Artifact store failed to initialize" for a healthy
        first launch and for a config setting somebody chose, which
        are three different situations wearing one sentence.
        """
        mgr = self._session.artifact_mgr
        status = await mgr.status()
        if status.enabled:
            return CommandResult.for_action(CommandAction.ARTIFACTS)

        record = getattr(self._session, "subsystems", None)
        entry = record.get(ARTIFACTS) if record is not None else None

        if entry is not None and entry.state is SubsystemState.PREPARING:
            return CommandResult.info(
                f"Setting up the artifact store — {entry.reason}. "
                "It'll be ready shortly; the rest of igni works meanwhile."
            )
        if entry is not None and entry.state is SubsystemState.FAILED:
            return CommandResult.error(
                f"Artifacts failed to load: {entry.reason}"
                + (f"\n\n{entry.fix}" if entry.fix else "")
            )
        if entry is not None and entry.state is SubsystemState.DISABLED:
            return CommandResult.info(f"Artifact store is disabled — {entry.reason}. {entry.fix}")

        # No record at all. Happens for a session built outside the
        # backend's attach path (tests, an embedded Session); fall back
        # to what config says rather than inventing a failure.
        if self._session.settings.artifacts.enabled:
            return CommandResult.error("Artifact store failed to initialize.")
        return CommandResult.info(
            "Artifact store is disabled. Set knowledge.enabled=true in config."
        )


async def cmd_artifacts(handler: CommandHandler, args: str) -> CommandResult:
    """Handle ``/artifacts`` commands.

    Two-line shim so :mod:`ember_code.backend.command_handler`
    keeps importing ``cmd_artifacts`` by name and calling it
    with ``(self, args)``. All real work lives on
    :class:`ArtifactsCommand`.
    """
    return await ArtifactsCommand(handler.session).dispatch(args)

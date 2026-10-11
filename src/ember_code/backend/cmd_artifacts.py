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
        * ``project [link | use <id>]`` → :meth:`project`
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
            case "project":
                return await self.project(sub_args)
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

    async def project(self, sub_args: str) -> CommandResult:
        """Which document project this working copy uses, and why; or change it.

        * no argument → the project, its remotes, and this working copy's remote
        * ``link`` → point this working copy's remote at its current project,
          moving it from whichever project had it (the fix for a conflict)
        * ``use <project id>`` → make this working copy use another project
        """
        resolver = getattr(self._session, "artifact_project", None)
        shared = getattr(getattr(self._session, "artifacts", None), "_shared", None)
        if resolver is None or shared is None or not getattr(shared, "is_configured", False):
            return CommandResult.error(
                "No document store is connected to this session, so there is no project to show."
            )
        verb, _, rest = sub_args.partition(" ")
        verb = verb.lower()

        if verb == "use":
            target = rest.strip()
            if not target:
                return CommandResult.error("Name the project: `/artifacts project use <project id>`.")
            binding = await resolver.use(target)
            if binding is None or binding.project_id != target:
                return CommandResult.error(f"No project {target} in your organisation.")
            return CommandResult.info(f"This working copy now uses '{binding.name}' ({binding.project_id}).")

        binding = await resolver.binding(force=verb == "link")
        if binding is None:
            return CommandResult.error(
                "Could not reach the document server to find this working copy's project; "
                "documents are searched unscoped until it answers."
            )

        if verb == "link":
            remote = resolver.remote()
            if not remote:
                return CommandResult.error("This working copy has no `origin` remote to link.")
            linked = await shared.link_remote(project_id=binding.project_id, remote=remote)
            if linked is None:
                return CommandResult.error("The project is no longer there; run `/artifacts project` again.")
            resolver.invalidate()
            return CommandResult.info(
                f"{linked.get('remote_key') or remote} now opens '{binding.name}' for everyone in your organisation."
            )

        remotes = ", ".join(binding.remotes) or "none yet"
        lines = [
            f"Document project: {binding.name} ({binding.project_id})",
            f"Known remotes: {remotes}",
            f"This working copy's remote: {resolver.remote() or 'none'}",
        ]
        if binding.notice and binding.status == "conflict":
            lines.append(binding.notice)
        return CommandResult.info("\n".join(lines))

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

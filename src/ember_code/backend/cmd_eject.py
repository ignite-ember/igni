"""``/eject`` — take a copy of something the group ships.

A group's entries are read from the policy cache, and anything the
project declares under the same name outranks them. That is the whole
override mechanism, and it has one rough edge: to change one line of the
org's ``reviewer`` agent you have to write a whole file from scratch, or
go digging in ``~/.igni/group-policy`` for something to copy.

This is that copy, made deliberately. It writes the group's version into
the project where the loaders already look, and from then on yours is
the one that loads.

## Why a command and not a sync

Group entries used to be copied into the project automatically, which
made overriding easy and everything else hard: the copy had to be kept
in step with the server's, which meant checksums to tell an edit from a
stale file, conflict records for when both sides moved, and a prompt
asking "yours or theirs". All of it existed to manage a copy nobody had
asked for.

Asking is the difference. One file, when somebody wants one, and no
machinery to keep it honest — if the group's version changes afterwards,
yours keeps winning, which is what "I took a copy" should mean.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ember_code.backend.command_result import CommandResult
from ember_code.core.paths import CONFIG_DIR

if TYPE_CHECKING:
    from ember_code.backend.command_handler import CommandHandler

logger = logging.getLogger(__name__)

#: kind → (subdirectory under the project's config dir, filename).
#:
#: Only the kinds a person would open and change. Hooks, MCP servers,
#: tools, plugins, settings and scripts are configuration the server
#: owns: a project can already declare its own without needing a copy of
#: the group's to start from, and for hooks and rules there is nothing to
#: override anyway — both are additive.
EJECTABLE: dict[str, tuple[str, str]] = {
    "agents": ("agents", "{name}.md"),
    "skills": ("skills", "{name}/SKILL.md"),
    "commands": ("commands", "{name}.md"),
    "output-styles": ("output-styles", "{name}.md"),
    "workflows": ("workflows", "{name}.mjs"),
}


class EjectCommand:
    """Copy one group entry into the project.

    Takes a :class:`Session` directly rather than the handler, mirroring
    the sibling coordinators.
    """

    def __init__(self, session) -> None:
        self._session = session

    async def run(self, args: str) -> CommandResult:
        parts = args.split()
        if len(parts) != 2:
            return CommandResult.error(
                "Usage: /eject <kind> <name>\n\n"
                f"Kinds: {', '.join(sorted(EJECTABLE))}\n"
                "Copies what your group ships into this project, where your "
                "copy outranks theirs."
            )

        kind, name = parts[0].strip().lower(), parts[1].strip()
        if kind not in EJECTABLE:
            return CommandResult.error(
                f"Cannot eject {kind!r}. Kinds: {', '.join(sorted(EJECTABLE))}.\n"
                "Hooks and rules are additive — a project's are added to the "
                "group's rather than replacing them, so there is nothing to "
                "take a copy of."
            )

        source_dir = self._session.group_root(kind)
        if source_dir is None:
            return CommandResult.error(
                f"Your group ships no {kind}. Nothing to eject."
            )

        subdir, filename = EJECTABLE[kind]
        relative = filename.format(name=name)
        source = source_dir / relative
        if not source.is_file():
            available = sorted(
                p.stem if kind != "skills" else p.parent.name
                for p in source_dir.rglob("*")
                if p.is_file()
            )
            return CommandResult.error(
                f"Your group ships no {kind} called {name!r}.\n"
                + (f"It ships: {', '.join(available)}." if available else "")
            )

        target = Path(self._session.project_dir) / CONFIG_DIR / subdir / relative
        if target.exists():
            return CommandResult.info(
                f"{target} already exists — that copy is already the one that "
                "loads. Edit it, or delete it to go back to your group's."
            )

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not eject %s %s: %s", kind, name, exc)
            return CommandResult.error(f"Could not write {target}: {exc}")

        # Rebuild so the copy is live in this session rather than at the
        # next start — the point of ejecting is usually to change it now.
        reloaded = False
        try:
            reloaded = bool(self._session.reload_group_agents())
        except Exception as exc:  # noqa: BLE001 — a copy that landed is still a success
            logger.debug("Reload after eject failed: %s", exc)

        return CommandResult.info(
            f"Copied your group's {name!r} to {target.relative_to(self._session.project_dir)}.\n"
            "Yours loads from now on; the group's changes no longer reach you for this one."
            + ("" if reloaded else "\nRestart the session to pick it up.")
        )


async def cmd_eject(handler: CommandHandler, args: str) -> CommandResult:
    """See :meth:`EjectCommand.run`."""
    return await EjectCommand(handler.session).run(args)

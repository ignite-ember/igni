"""Move a ``.ember`` directory to ``.igni``, once, without losing anything.

The product is called igni. The directory was called ``.ember`` because
the package still is, and there are two of them: one under the user's
home holding credentials and state, one under each project holding
configuration.

Renaming the home one is the part that can hurt. It holds
``credentials.json`` and ``client_state.db``, so a rename that half
happens signs somebody out; a rename that never happens makes a working
install look like a fresh one, because everything would be looking at a
``~/.igni`` that is not there.

So this is deliberately the most boring code that could work:

* **Rename, never merge.** If the destination already exists, stop and
  leave both alone. Two directories with overlapping state is a problem
  nobody can unpick afterwards, and "you have both, pick one" is a
  better outcome than a silent interleave.
* **Never follow a symlink.** ``ember-server/.ember`` in this workspace
  is a symlink to ``ember-code/.ember``; renaming through one would move
  somebody else's directory and leave a dangling link behind.
* **Idempotent.** Nothing to do is the normal case — after the first run
  there is no legacy directory, and this costs two ``stat`` calls.
* **Never raises.** A migration that cannot run must not stop a session
  starting. The legacy directory stays where it is and the next run
  tries again.

It is also not the only line of defence: :func:`ember_code.core.paths`
falls back to the legacy directory when the new one is absent, so a
machine where this never ran still finds its credentials. The rename is
tidiness; the fallback is correctness.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ember_code.core.paths import CONFIG_DIR, LEGACY_CONFIG_DIR

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MigrationOutcome:
    """What happened, in terms a log line can use."""

    moved: bool
    #: Why not, when ``moved`` is False. None when it did move.
    reason: str | None = None
    source: Path | None = None
    destination: Path | None = None

    def __str__(self) -> str:  # pragma: no cover — logging convenience
        if self.moved:
            return f"renamed {self.source} to {self.destination}"
        return self.reason or "nothing to do"


def migrate_directory(parent: Path) -> MigrationOutcome:
    """Rename ``<parent>/.ember`` to ``<parent>/.igni`` when it is safe."""
    legacy = parent / LEGACY_CONFIG_DIR
    target = parent / CONFIG_DIR

    if not legacy.exists():
        return MigrationOutcome(moved=False, reason="no legacy directory")

    # ``is_symlink`` before ``is_dir``: the latter follows the link and
    # would answer True for a symlink to a directory.
    if legacy.is_symlink():
        return MigrationOutcome(
            moved=False,
            reason=f"{legacy} is a symlink; leaving it for somebody who knows where it points",
        )

    if not legacy.is_dir():
        return MigrationOutcome(moved=False, reason=f"{legacy} is not a directory")

    if target.exists():
        return MigrationOutcome(
            moved=False,
            reason=(
                f"both {target.name} and {legacy.name} exist under {parent}; "
                f"leaving both alone — merging them is not something this can "
                f"decide safely"
            ),
        )

    try:
        legacy.rename(target)
    except OSError as exc:
        # Includes the cross-device case, where rename is not atomic and
        # a copy would have to be resumable to be safe. Not worth it: the
        # fallback in ``paths`` means an unmigrated directory still works.
        return MigrationOutcome(moved=False, reason=f"could not rename {legacy}: {exc}")

    return MigrationOutcome(moved=True, source=legacy, destination=target)


def migrate_home() -> MigrationOutcome:
    """Rename ``~/.ember`` to ``~/.igni``. Safe to call on every start."""
    outcome = migrate_directory(Path.home())
    if outcome.moved:
        logger.info("Config directory %s", outcome)
    else:
        logger.debug("Home config directory: %s", outcome)
    return outcome


def migrate_project(project_dir: Path) -> MigrationOutcome:
    """Rename ``<project>/.ember`` to ``<project>/.igni``.

    Separate from :func:`migrate_home` only so the caller can say which
    one it meant in a log line — a person reading "renamed" wants to know
    whether their home directory or their repository just moved.
    """
    outcome = migrate_directory(Path(project_dir))
    if outcome.moved:
        logger.info("Project config directory %s", outcome)
    else:
        logger.debug("Project config directory: %s", outcome)
    return outcome

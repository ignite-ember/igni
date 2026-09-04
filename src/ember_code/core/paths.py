"""The names igni writes down, in one place.

There are two of these and they hold different things:

* ``<project>/<CONFIG_DIR>`` — the project's own configuration, and the
  files a team would share.
* ``~/<CONFIG_DIR>`` — this machine's state and credentials.

Both were spelled out as the bare string ``".ember"`` in 71 places
across 110 files, which made "the configuration directory" something
you could not grep for as a concept — only as a string that also
appears in prose, in ``ember.md``, and in unrelated paths. Renaming it
meant finding all of them and being sure each one was the directory and
not a sentence about it.

Deliberately the shallowest module in ``core``: it imports nothing, so
anything can import it without a cycle. Prose in docstrings still spells
the name out, because a docstring reading ``~/{CONFIG_DIR}/config.yaml``
helps nobody — those are rewritten when the value changes, not
parameterised.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: The directory igni keeps configuration in, under a project root and
#: under the user's home.
CONFIG_DIR = ".igni"

#: What it was called before the product settled on its name. Still read
#: when the new one is absent, so an install that has not been migrated
#: finds its credentials rather than looking brand new — see
#: :func:`home_config_dir` and
#: :mod:`ember_code.core.dir_migration`.
LEGACY_CONFIG_DIR = ".ember"

#: The home directory's spelling, unexpanded — ``data_dir`` defaults to
#: this in 37 signatures, which is why it is named rather than repeated.
#: Left as a ``str`` because that is what those parameters take, and
#: ``Path("~/...")`` is a trap: it does not expand itself.
#:
#: Deliberately *not* resolved through :func:`home_config_dir`: this is a
#: default for helper signatures, and a module constant that inspected
#: the filesystem at import time would answer differently depending on
#: when it was imported. The migration runs before anything reads it.
DEFAULT_DATA_DIR = f"~/{CONFIG_DIR}"

#: The sibling read for cross-tool compatibility. Named alongside
#: ``CONFIG_DIR`` because the two are always considered together: every
#: loader that scans one scans the other when cross-tool support is on.
CLAUDE_CONFIG_DIR = ".claude"

#: The project's context file — the one a team writes and commits.
#:
#: The directory rename above stopped at the directory. This file was
#: still ``ember.md``: created by ``igni init``, named in the settings
#: default, and hardcoded in seven more tuples — so a product called
#: igni wrote a file called ember into every project a customer
#: initialised. Finishing the rename is the same change, one file later.
PROJECT_CONTEXT_FILE = "igni.md"

#: Still read when present, exactly as ``CLAUDE.md`` is.
#:
#: Not a compatibility shim to be swept away later: the context loaders
#: have always read a *list* of filenames, because a repository may
#: carry another tool's context file and there is no reason to ignore
#: it. ``ember.md`` joins that list rather than being special — a team
#: with one in git keeps it working, and nothing has to migrate.
LEGACY_PROJECT_CONTEXT_FILE = "ember.md"

#: Every project-context filename, in precedence order: ours, the name
#: ours used to have, then the sibling tool's.
#:
#: One tuple because it was spelled out in five places, and two of them
#: carried a comment asking a human to "keep in lockstep with" the
#: other — which is the shape that has drifted every other time this
#: review has found it. Now there is nothing to keep in step.
PROJECT_CONTEXT_FILES = (PROJECT_CONTEXT_FILE, LEGACY_PROJECT_CONTEXT_FILE)

#: With the sibling tool's, for when cross-tool support is on.
PROJECT_CONTEXT_FILES_CROSS_TOOL = (*PROJECT_CONTEXT_FILES, "CLAUDE.md")


def _with_local_overrides(names: tuple[str, ...]) -> tuple[str, ...]:
    """``a.md`` → ``a.md, a.local.md`` for each name, order preserved.

    The ``.local.md`` sibling is the personal override of a committed
    file (the convention is to gitignore it), and it must load *after*
    its committed partner so its content wins in the concatenation the
    model reads top to bottom. Interleaving rather than appending all
    the local ones at the end is what preserves that pairing.
    """
    out: list[str] = []
    for name in names:
        stem, _, suffix = name.rpartition(".")
        out.append(name)
        out.append(f"{stem}.local.{suffix}")
    return tuple(out)


#: The rules-file variants, including personal overrides.
RULES_FILES = _with_local_overrides(PROJECT_CONTEXT_FILES)
RULES_FILES_CROSS_TOOL = _with_local_overrides(PROJECT_CONTEXT_FILES_CROSS_TOOL)

#: The top-level agent's name. User-visible in the TUI, and persisted as
#: ``agent_id`` on every saved session — which is what makes it more
#: than a label: ``persistence.listing`` uses it to tell the user's own
#: chats from the scratch sessions sub-agents write to the same
#: database.
MAIN_AGENT_NAME = "igni"

#: What it was called before. Sessions saved under the old name are
#: still listed, for the reason the listing module already gives about
#: rows from earlier versions: a rename that hid somebody's history
#: would be a worse outcome than the wrong name in a header.
LEGACY_MAIN_AGENT_NAMES = ("ember",)

#: Every ``agent_id`` that means "this was the user talking to igni".
MAIN_AGENT_IDS = (MAIN_AGENT_NAME, *LEGACY_MAIN_AGENT_NAMES)


def managed_policy_dir() -> Path | None:
    """Where a sysadmin or MDM drops org-wide policy, or None off-platform.

    One function because four modules used to answer this separately and
    two of them disagreed — on *every* platform:

    ==========  ===============================  ==============================
    platform    managed_policy / plugins / rules  mcp
    ==========  ===============================  ==============================
    macOS       ``…/Application Support/Ember``   ``…/Application Support/EmberCode``
    Linux       ``/etc/ember``                    ``/etc/ignite-ember``
    Windows     ``%PROGRAMDATA%/Ember``           unsupported
    ==========  ===============================  ==============================

    So an admin deploying managed settings had to populate two directory
    trees to cover both, MCP policy could not be deployed on Windows at
    all, and nothing said so. Managed policy is the tier a user is not
    allowed to override, which makes "it silently was not read" the worst
    possible failure for it.

    Returns None on unknown platforms; every caller treats that as "no
    managed tier", which is the safe reading — absent policy, not empty
    policy.
    """
    if sys.platform == "darwin":
        return Path("/Library/Application Support/igni")
    if sys.platform.startswith("linux"):
        return Path("/etc/igni")
    if sys.platform == "win32":
        program_data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
        return Path(program_data) / "igni"
    return None


def project_config_dir(project_dir: Path | str) -> Path:
    """``<project>/.igni``, or ``<project>/.ember`` while that is the one
    on disk.

    Falls back for the same reason :func:`home_config_dir` does, and the
    project case is the one with a real ordering hazard: settings are
    loaded *before* a Session exists, and the project migration runs
    inside ``ProjectInitializer`` during Session construction. Without
    this, the first run after an upgrade reads no project config at all
    — silently falling back to the built-in defaults, so the session
    comes up on the wrong model with the wrong guardrails and says
    nothing about it.

    With the fallback, the ordering stops mattering and the migration is
    tidiness in both places rather than a step something depends on.
    """
    root = Path(project_dir)
    current = root / CONFIG_DIR
    if current.exists():
        return current
    legacy = root / LEGACY_CONFIG_DIR
    if legacy.exists():
        return legacy
    return current


def home_config_dir() -> Path:
    """``~/.igni``, or ``~/.ember`` while that is the one on disk.

    The fallback is what makes the rename safe rather than load-bearing.
    ``~/.ember`` holds ``credentials.json`` and ``client_state.db``, so a
    machine where the migration has not run — or could not, across
    filesystems — must still find them. Looking only at the new name
    would make a working install indistinguishable from a fresh one.

    Prefers the new name when both exist, which is the state the
    migration deliberately refuses to resolve: it will not merge two
    directories, so somebody has to, and until they do the new one is
    the one in use.
    """
    home = Path.home()
    current = home / CONFIG_DIR
    if current.exists():
        return current
    legacy = home / LEGACY_CONFIG_DIR
    if legacy.exists():
        return legacy
    return current

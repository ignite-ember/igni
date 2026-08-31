"""The name of igni's configuration directory, in one place.

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

from pathlib import Path

#: The directory igni keeps configuration in, under a project root and
#: under the user's home.
CONFIG_DIR = '.igni'

#: What it was called before the product settled on its name. Still read
#: when the new one is absent, so an install that has not been migrated
#: finds its credentials rather than looking brand new — see
#: :func:`home_config_dir` and
#: :mod:`ember_code.core.dir_migration`.
LEGACY_CONFIG_DIR = '.ember'

#: The home directory's spelling, unexpanded — ``data_dir`` defaults to
#: this in 37 signatures, which is why it is named rather than repeated.
#: Left as a ``str`` because that is what those parameters take, and
#: ``Path("~/...")`` is a trap: it does not expand itself.
#:
#: Deliberately *not* resolved through :func:`home_config_dir`: this is a
#: default for helper signatures, and a module constant that inspected
#: the filesystem at import time would answer differently depending on
#: when it was imported. The migration runs before anything reads it.
DEFAULT_DATA_DIR = f'~/{CONFIG_DIR}'

#: The sibling read for cross-tool compatibility. Named alongside
#: ``CONFIG_DIR`` because the two are always considered together: every
#: loader that scans one scans the other when cross-tool support is on.
CLAUDE_CONFIG_DIR = '.claude'


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

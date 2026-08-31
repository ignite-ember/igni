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
#: under the user's home. Change it here and both move.
CONFIG_DIR = '.ember'

#: The home directory's spelling, unexpanded — ``data_dir`` defaults to
#: this in 37 signatures, which is why it is named rather than repeated.
#: Left as a ``str`` because that is what those parameters take, and
#: ``Path("~/...")`` is a trap: it does not expand itself.
DEFAULT_DATA_DIR = f'~/{CONFIG_DIR}'

#: The sibling read for cross-tool compatibility. Named alongside
#: ``CONFIG_DIR`` because the two are always considered together: every
#: loader that scans one scans the other when cross-tool support is on.
CLAUDE_CONFIG_DIR = '.claude'


def project_config_dir(project_dir: Path | str) -> Path:
    """``<project_dir>/<CONFIG_DIR>``, unexpanded."""
    return Path(project_dir) / CONFIG_DIR


def home_config_dir() -> Path:
    """``~/<CONFIG_DIR>``, expanded."""
    return Path.home() / CONFIG_DIR

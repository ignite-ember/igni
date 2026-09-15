"""Every tool module imports on its own.

``ember_code.core.tools.plan`` did not. Importing it raised

    ImportError: cannot import name 'PlanTool' from partially initialized
    module 'ember_code.core.tools.plan'

because ``plan/submission.py`` imported ``BroadcastEvent`` from
``core.session.broadcast_schema``, and importing any submodule of
``core.session`` runs that package's ``__init__``, which reaches
``core → agent_builder → tools_builder`` and back into ``plan``. A tool
importing the session is the wrong direction; the schema was simply in the wrong
package, and it is a leaf with no ``ember_code`` imports of its own, so it now
lives at ``core.broadcast_schema`` where anything may import it.

It worked in the app, which is why it went unnoticed: the real entry points pull
the package in an order that resolves the cycle before anything needs it. It
broke exactly where the order is not guaranteed — a tool enumerating the
toolkits by walking ``core.tools``. That enumeration swallowed the failure and
carried on, so ``enter_plan_mode`` silently vanished from a list whose whole
purpose was completeness, and a missing tool reads identically to a tool that
does not exist.

This is the cheap version of that lesson: a module that only imports inside a
blessed order is one refactor away from a confident wrong answer somewhere else.
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest

import ember_code.core.tools as tools_pkg

_MODULES = sorted(
    m.name for m in pkgutil.walk_packages(tools_pkg.__path__, tools_pkg.__name__ + ".")
)


def test_there_are_modules_to_check():
    """A walk that finds nothing would make every case below vacuous."""
    assert len(_MODULES) > 20, _MODULES


@pytest.mark.parametrize("module", _MODULES)
def test_it_imports_without_help(module: str):
    """Imported in a fresh interpreter's worth of ordering, not after the app.

    ``importlib.import_module`` reuses ``sys.modules``, so under a full test run
    most of these are already loaded and this asserts little on its own. It
    still catches the failure when the module is reached first — which is the
    situation the tool enumerator was in, and the one that lost
    ``enter_plan_mode``.
    """
    importlib.import_module(module)


def test_the_plan_package_specifically(subprocess_python=None):  # noqa: ARG001
    """The regression, in a genuinely cold interpreter.

    Run out-of-process on purpose. The parametrised cases above share this
    process with every other test, so by the time they run the import order has
    long since been established by something else and the cycle cannot reappear.
    A subprocess is the only place the original failure is reproducible.
    """
    import subprocess
    import sys

    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", "import ember_code.core.tools.plan"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"ember_code.core.tools.plan does not import on its own:\n{result.stderr.strip()[-600:]}"
    )

"""A slash command's module loads when somebody runs it, not at start.

The registry holds each built-in as ``"module:attr"`` and imports it on
first dispatch, so an idle backend carries none of what ``/evals``,
``/loop``, ``/schedule`` or ``/plugin`` need. These tests pin that from
both ends: listing never imports a command, every entry still resolves
to a real shim, and a backend that has started and sat idle has not
imported the command modules or alembic.
"""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from ember_code.backend.builtin_command_registry import BUILTIN_REGISTRY

#: Loaded by commands (or by migrations), never by an idle backend.
NOT_AT_START = (
    "alembic",
    "ember_code.core.evals",
    "ember_code.core.db.migrations",
    "ember_code.backend.cmd_evals",
    "ember_code.backend.cmd_loop",
    "ember_code.backend.cmd_schedule",
    "ember_code.backend.cmd_plugin",
    "ember_code.backend.cmd_auth",
    "ember_code.backend.cmd_agents",
    "ember_code.backend.cmd_memory",
)


def _loaded(modules: list[str], name: str) -> bool:
    return any(m == name or m.startswith(name + ".") for m in modules)


def test_listing_and_describing_import_no_command():
    script = textwrap.dedent(
        """
        import json, sys
        from ember_code.backend.builtin_command_registry import BUILTIN_REGISTRY
        names = BUILTIN_REGISTRY.names()
        described = [BUILTIN_REGISTRY.describe(n) for n in names]
        assert all(described), "every built-in has a description"
        print(json.dumps(sorted(m for m in sys.modules if m.startswith("ember_code.backend.cmd_"))))
        """
    )
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    ).stdout
    assert json.loads(out.strip().splitlines()[-1]) == []


@pytest.mark.parametrize("entry", BUILTIN_REGISTRY.entries(), ids=lambda e: e.slash)
def test_every_entry_resolves_to_a_coroutine_shim(entry):
    shim = entry.resolve()
    assert inspect.iscoroutinefunction(shim), entry.target
    assert entry.resolve() is shim, "resolved once and kept"
    assert entry.module.startswith("ember_code.backend.cmd_")


def test_a_shim_that_takes_no_args_is_called_without_them():
    entry = BUILTIN_REGISTRY.get("/whoami")
    assert entry is not None and not entry.takes_args
    params = inspect.signature(entry.resolve()).parameters
    assert len(params) == 2, "the registry adapts it to (handler, args)"


# A backend started the way the app starts it, with a thread that dumps
# ``sys.modules`` once the test asks — after it is ready and has idled.
_RUNNER = textwrap.dedent(
    """
    import json, os, runpy, sys, threading, time

    def dump_when_asked():
        trigger, out = os.environ["DUMP_TRIGGER"], os.environ["DUMP_OUT"]
        while not os.path.exists(trigger):
            time.sleep(0.05)
        with open(out + ".tmp", "w") as f:
            json.dump(sorted(sys.modules), f)
        os.replace(out + ".tmp", out)

    threading.Thread(target=dump_when_asked, daemon=True).start()
    sys.argv = ["ember_code.backend", "--ws-port", "0"]
    runpy.run_module("ember_code.backend", run_name="__main__", alter_sys=True)
    """
)


def test_an_idle_backend_has_not_loaded_the_commands_or_alembic(tmp_path: Path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    env = dict(
        os.environ,
        HOME=str(home),
        XDG_CONFIG_HOME=str(home / ".config"),
        XDG_DATA_HOME=str(home / ".local" / "share"),
        DUMP_TRIGGER=str(tmp_path / "dump"),
        DUMP_OUT=str(tmp_path / "modules.json"),
    )
    # Prepared as the cloud prepares a volume, so the start finds its
    # database current and has no reason to migrate.
    subprocess.run(
        [sys.executable, "-m", "ember_code", "migrate"],
        cwd=project,
        env=env,
        check=True,
        capture_output=True,
    )
    backend = subprocess.Popen(
        [sys.executable, "-c", _RUNNER],
        cwd=project,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert backend.stdout is not None
        ready = json.loads(backend.stdout.readline())
        assert ready["status"] == "ready"
        time.sleep(1.0)
        (tmp_path / "dump").touch()
        out = tmp_path / "modules.json"
        deadline = time.monotonic() + 15
        while not out.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        modules = json.loads(out.read_text())
    finally:
        backend.kill()
        backend.wait(10)

    assert _loaded(modules, "ember_code.backend.app"), "it really started"
    leaked = [name for name in NOT_AT_START if _loaded(modules, name)]
    assert leaked == [], f"loaded at start: {leaked}"

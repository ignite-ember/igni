"""The ``python`` packaging: a private environment, from the wheelhouse only.

igni server's ``docs/service-identity.md`` §8. A service's extension is a Python
package whose bundle carries every wheel it needs; igni installs it
into an environment of its own, made with igni's own interpreter, and
never asks an index — an air-gapped deployment does not have one, and
a machine that does must not quietly use it.

The real install here runs pip against a wheel this file writes by
hand, with ``PIP_INDEX_URL`` pointing at an unroutable host: it can
only pass offline. The development path — building the package from
a source directory, network allowed — is checked as the commands it
runs, because a real build needs a build backend from an index.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from ember_code.extensions.installer import ExtensionInstaller
from ember_code.extensions.manifest import ExtensionManifest
from ember_code.extensions.python_env import (
    READY_MARKER,
    CommandOutcome,
    CommandRunner,
    PythonEnvironmentError,
    PythonEnvironments,
)
from ember_code.extensions.registry import ExtensionRegistry, ExtensionState
from ember_code.extensions.rpc import ExtensionRpc
from ember_code.extensions.store import DistributionRecord
from tests.extension_distribution_helpers import build_wheel, manifest


def bundle_with_wheels(tmp_path: Path, version: str = "1.0.0", **changes: Any) -> Path:
    bundle = tmp_path / f"bundle-{version}"
    build_wheel(bundle / "wheels", version=version)
    (bundle / "manifest.json").write_text(json.dumps(manifest(version=version, **changes)))
    return bundle


def load(bundle: Path) -> ExtensionManifest:
    return ExtensionManifest.load(bundle / "manifest.json")


class Counting(CommandRunner):
    """The real runner, counted."""

    def __init__(self) -> None:
        self.argv: list[list[str]] = []

    async def run(self, argv, *, env=None, cwd=None) -> CommandOutcome:
        self.argv.append(list(argv))
        return await super().run(argv, env=env, cwd=cwd)


class Pretending(CommandRunner):
    """Records what would run, and makes the environment look built."""

    def __init__(self, entry: str = "acme-notes") -> None:
        self.calls: list[tuple[list[str], dict[str, str] | None]] = []
        self._entry = entry

    async def run(self, argv, *, env=None, cwd=None) -> CommandOutcome:
        self.calls.append((list(argv), env))
        if argv[1:3] == ["-m", "venv"]:
            environment = Path(argv[3])
            PythonEnvironments.executable(environment, "python").parent.mkdir(parents=True)
        if "install" in argv:
            python = Path(argv[0])
            script = python.parent / self._entry
            script.write_text("#!/bin/sh\n")
        return CommandOutcome(returncode=0, output="")


@pytest.fixture
def no_index(monkeypatch: pytest.MonkeyPatch) -> None:
    """An index that does not exist. Reaching for it fails the install."""
    monkeypatch.setenv("PIP_INDEX_URL", "http://placeholder.invalid/simple")
    monkeypatch.setenv("PIP_EXTRA_INDEX_URL", "http://placeholder.invalid/extra")


async def test_a_wheelhouse_installs_offline_and_its_entry_runs(tmp_path, no_index):
    bundle = bundle_with_wheels(tmp_path)
    environments = PythonEnvironments(tmp_path / "envs")

    entry = await environments.ensure(load(bundle), bundle, development=False)

    assert entry.is_file()
    assert entry.parent.parent == tmp_path / "envs" / "acme.notes" / "1.0.0"
    process = await asyncio.create_subprocess_exec(
        str(entry), stdout=asyncio.subprocess.PIPE, env={"PATH": "/usr/bin:/bin"}
    )
    out, _ = await process.communicate()
    assert json.loads(out) == {"hello": "from acme-notes"}


async def test_the_environment_is_built_with_igni_s_own_interpreter(tmp_path):
    bundle = bundle_with_wheels(tmp_path)
    runner = Pretending()
    await PythonEnvironments(tmp_path / "envs", runner=runner).ensure(
        load(bundle), bundle, development=False
    )
    venv = runner.calls[0][0]
    assert venv[:3] == [sys.executable, "-m", "venv"]


async def test_the_install_reaches_no_index(tmp_path, no_index):
    bundle = bundle_with_wheels(tmp_path)
    runner = Pretending()
    await PythonEnvironments(tmp_path / "envs", runner=runner).ensure(
        load(bundle), bundle, development=False
    )
    argv, env = runner.calls[-1]
    assert "--no-index" in argv
    assert argv[argv.index("--find-links") + 1] == str((bundle / "wheels").resolve())
    assert argv[argv.index("--only-binary") + 1] == ":all:"
    assert argv[-1] == "acme-notes"
    assert env is not None
    assert "PIP_INDEX_URL" not in env and "PIP_EXTRA_INDEX_URL" not in env
    assert env["PIP_CONFIG_FILE"] == os.devnull


async def test_a_built_environment_is_reused(tmp_path):
    bundle = bundle_with_wheels(tmp_path)
    runner = Counting()
    environments = PythonEnvironments(tmp_path / "envs", runner=runner)
    first = await environments.ensure(load(bundle), bundle, development=False)
    built = len(runner.argv)
    second = await environments.ensure(load(bundle), bundle, development=False)
    assert first == second
    assert len(runner.argv) == built, "a finished environment must not be rebuilt"


async def test_an_interrupted_environment_is_rebuilt(tmp_path):
    bundle = bundle_with_wheels(tmp_path)
    runner = Pretending()
    environments = PythonEnvironments(tmp_path / "envs", runner=runner)
    await environments.ensure(load(bundle), bundle, development=False)
    (environments.path_for(load(bundle)) / READY_MARKER).unlink()
    runner.calls.clear()
    await environments.ensure(load(bundle), bundle, development=False)
    assert runner.calls, "no marker means unfinished, and unfinished is rebuilt"


async def test_a_new_version_drops_the_old_environment(tmp_path):
    runner = Pretending()
    environments = PythonEnvironments(tmp_path / "envs", runner=runner)
    old = bundle_with_wheels(tmp_path, "1.0.0")
    await environments.ensure(load(old), old, development=False)
    new = bundle_with_wheels(tmp_path, "1.1.0")
    await environments.ensure(load(new), new, development=False)
    assert sorted(p.name for p in (tmp_path / "envs" / "acme.notes").iterdir()) == ["1.1.0"]


async def test_a_distributed_bundle_without_wheels_is_refused(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "manifest.json").write_text(json.dumps(manifest()))
    (bundle / "pyproject.toml").write_text("[project]\nname='acme-notes'\n")
    runner = Pretending()
    with pytest.raises(PythonEnvironmentError, match="wheels only"):
        await PythonEnvironments(tmp_path / "envs", runner=runner).ensure(
            load(bundle), bundle, development=False
        )
    assert runner.calls == [], "nothing is built for a bundle that cannot install offline"


async def test_a_development_install_builds_the_wheelhouse_from_source(tmp_path):
    """Network allowed, and then the same offline install: the build
    step is the only difference, and it proves the directory builds
    the package the manifest names."""
    bundle = tmp_path / "source"
    bundle.mkdir()
    (bundle / "manifest.json").write_text(json.dumps(manifest()))
    (bundle / "pyproject.toml").write_text("[project]\nname='acme-notes'\n")
    runner = Pretending()

    await PythonEnvironments(tmp_path / "envs", runner=runner).ensure(
        load(bundle), bundle, development=True
    )
    build, build_env = runner.calls[1]
    assert build[1:4] == ["-m", "pip", "wheel"]
    assert build[-1] == str(bundle)
    assert "--no-index" not in build
    assert build_env is None, "a development build uses the developer's own pip setup"
    install, _ = runner.calls[2]
    assert "--no-index" in install
    assert install[install.index("--find-links") + 1] == build[build.index("--wheel-dir") + 1]


async def test_a_development_install_is_rebuilt_when_its_code_changes_under_one_version(tmp_path):
    """Reinstalling changed code under the same version must not run the old code.

    Keyed by version, it did: the environment for ``1.0.0`` was found
    finished and reused. A development install is keyed by what it is
    made of.
    """
    bundle = tmp_path / "source"
    (bundle / "src").mkdir(parents=True)
    (bundle / "manifest.json").write_text(json.dumps(manifest()))
    (bundle / "pyproject.toml").write_text("[project]\nname='acme-notes'\n")
    (bundle / "src" / "notes.py").write_text("ANSWER = 1\n")
    runner = Pretending()
    environments = PythonEnvironments(tmp_path / "envs", runner=runner)

    await environments.ensure(load(bundle), bundle, development=True)
    runner.calls.clear()
    # The same code again — and caches that do not count — is reused.
    (bundle / "src" / "__pycache__").mkdir()
    (bundle / "src" / "__pycache__" / "notes.cpython-312.pyc").write_bytes(b"x")
    await environments.ensure(load(bundle), bundle, development=True)
    assert runner.calls == [], "unchanged code is not rebuilt"

    (bundle / "src" / "notes.py").write_text("ANSWER = 2\n")
    await environments.ensure(load(bundle), bundle, development=True)
    assert any(call[1:4] == ["-m", "pip", "wheel"] for call, _ in runner.calls), (
        "changed code under the same version is rebuilt"
    )


async def test_a_distributed_bundle_is_still_keyed_by_its_version(tmp_path):
    bundle = bundle_with_wheels(tmp_path)
    runner = Pretending()
    environments = PythonEnvironments(tmp_path / "envs", runner=runner)
    await environments.ensure(load(bundle), bundle, development=False)
    runner.calls.clear()
    (bundle / "wheels" / "extra.txt").write_text("not part of what was verified")
    await environments.ensure(load(bundle), bundle, development=False)
    assert runner.calls == []


async def test_a_wheelhouse_outside_the_bundle_is_refused(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    raw = manifest()
    raw["python"]["wheels"] = "../elsewhere"
    (bundle / "manifest.json").write_text(json.dumps(raw))
    with pytest.raises(PythonEnvironmentError, match="outside its bundle"):
        await PythonEnvironments(tmp_path / "envs", runner=Pretending()).ensure(
            load(bundle), bundle, development=False
        )


async def test_a_package_without_the_named_script_is_refused(tmp_path):
    bundle = bundle_with_wheels(tmp_path)
    raw = json.loads((bundle / "manifest.json").read_text())
    raw["python"]["entry"] = "not-there"
    (bundle / "manifest.json").write_text(json.dumps(raw))
    with pytest.raises(PythonEnvironmentError, match="no console script 'not-there'"):
        await PythonEnvironments(tmp_path / "envs", runner=Pretending()).ensure(
            load(bundle), bundle, development=False
        )


async def test_a_failing_pip_is_quoted(tmp_path):
    class Failing(CommandRunner):
        async def run(self, argv, *, env=None, cwd=None) -> CommandOutcome:
            if "install" in argv:
                return CommandOutcome(returncode=1, output="ERROR: No matching distribution")
            return await Pretending().run(argv, env=env, cwd=cwd)

    bundle = bundle_with_wheels(tmp_path)
    with pytest.raises(PythonEnvironmentError, match="No matching distribution"):
        await PythonEnvironments(tmp_path / "envs", runner=Failing()).ensure(
            load(bundle), bundle, development=False
        )


# ── Through the installer ──────────────────────────────────────────


class StubMcp:
    def __init__(self) -> None:
        self.configs: dict[str, Any] = {}

    async def connect(self, name: str) -> Any:
        return type("Ok", (), {"ok": True, "reason": ""})()

    async def disconnect_one(self, name: str) -> None:
        self.configs.pop(name, None)

    def get_client(self, name: str) -> None:
        return None

    def get_tools(self, name: str) -> dict[str, Any]:
        return {}

    def require_confirmation(self, server: str, tools: set[str]) -> None:
        pass

    async def tool_annotations(self, name: str) -> dict[str, Any]:
        return {}


async def test_the_installer_starts_the_installed_entry(tmp_path):
    bundle = bundle_with_wheels(tmp_path)
    registry = ExtensionRegistry()
    mcp = StubMcp()
    installer = ExtensionInstaller(
        registry=registry,
        rpc=ExtensionRpc(registry),
        mcp=mcp,
        environments=PythonEnvironments(tmp_path / "envs", runner=Pretending()),
    )
    entry = await installer.install(
        bundle,
        distribution=DistributionRecord(
            extension_id="acme.notes",
            version="1.0.0",
            sha256="a" * 64,
            service_url="https://notes.example.com",
        ),
    )
    assert entry.state is ExtensionState.READY, entry.reason
    config = mcp.configs["ext:acme.notes"]
    assert config.command == str(
        PythonEnvironments.executable(tmp_path / "envs" / "acme.notes" / "1.0.0", "acme-notes")
    )
    assert config.type.value == "stdio"
    assert config.cwd == str(bundle)


async def test_without_anywhere_to_install_it_is_failed_not_raised(tmp_path):
    bundle = bundle_with_wheels(tmp_path)
    registry = ExtensionRegistry()
    installer = ExtensionInstaller(
        registry=registry, rpc=ExtensionRpc(registry), mcp=StubMcp(), allow_development=True
    )
    entry = await installer.install(bundle)
    assert entry.state is ExtensionState.FAILED
    assert "nowhere to install" in entry.reason

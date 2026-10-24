"""What a backend serving a group will not install.

EXTENSIBILITY has said since it was written that a sidecar is
single-user only and that a shared backend serves no extension UI.
Both rules were right and neither was checked, so an administrator
could install the thing the document says does not work and find out
from the behaviour.

**Why one process cannot serve a group.** A sidecar — and the
development packaging, which is the same thing without a container —
is one supervised process, and everything igni tells it is singular:
one ``IGNI_WORKSPACE``, one resolution of each ``IGNI_SECRET_*``, one
settings file. On a backend serving several principals there is no
answer to *whose*.

And there is no principal on this path to answer with. ``caller`` is
an extension id, so the check is which extension is asking and never
which person — a shared process could not be made safe by being
careful, because the identity it would need does not exist. That is
the gap `docs/EXTENSION_READINESS.md` records; this file covers the
part that can be enforced today.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from ember_code.core.mcp.client import MCPClientManager
from ember_code.extensions.installer import ExtensionInstaller, InstallError
from ember_code.extensions.origin import ExtensionOrigins
from ember_code.extensions.profile import ENV_VAR, DeploymentProfile
from ember_code.extensions.registry import ExtensionRegistry, ExtensionState
from ember_code.extensions.rpc import ExtensionRpc

FIXTURE = Path(__file__).parent / "fixtures" / "dev_extension"


def bundle_at(tmp_path: Path, **changes: object) -> Path:
    bundle = tmp_path / "ext"
    shutil.copytree(FIXTURE, bundle)
    raw = json.loads((bundle / "manifest.json").read_text())
    raw["dev"]["command"] = [sys.executable, "server.py"]
    raw.update(changes)
    (bundle / "manifest.json").write_text(json.dumps(raw))
    return bundle


def installer(tmp_path: Path, profile: DeploymentProfile) -> ExtensionInstaller:
    registry = ExtensionRegistry()
    return ExtensionInstaller(
        registry=registry,
        rpc=ExtensionRpc(registry),
        mcp=MCPClientManager(project_dir=tmp_path),
        origins=ExtensionOrigins(),
        profile=profile,
        allow_development=True,
    )


# ── The profile itself ─────────────────────────────────────────────


def test_a_backend_is_single_user_unless_it_says_otherwise(monkeypatch):
    """The permissive answer must not be what you get by forgetting
    to configure anything."""
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert DeploymentProfile.current() is DeploymentProfile.SINGLE_USER


@pytest.mark.parametrize("value", ["shared", "multi", "multi-user", "SHARED"])
def test_shared_is_spelled_several_ways(monkeypatch, value):
    monkeypatch.setenv(ENV_VAR, value)
    assert DeploymentProfile.current().is_shared


def test_a_value_that_is_not_a_profile_falls_back_to_single_user(monkeypatch):
    """Loudly, in the log — but a backend that will not start is
    worse than one that is careful."""
    monkeypatch.setenv(ENV_VAR, "sometimes")
    assert DeploymentProfile.current() is DeploymentProfile.SINGLE_USER


# ── What a shared backend refuses ──────────────────────────────────


@pytest.mark.asyncio
async def test_a_process_packaging_is_refused_on_a_shared_backend(tmp_path):
    with pytest.raises(InstallError, match="more than one principal"):
        await installer(tmp_path, DeploymentProfile.SHARED).install(bundle_at(tmp_path))


@pytest.mark.asyncio
async def test_a_python_package_is_a_process_too(tmp_path):
    """A service's extension runs here as one process, so a shared
    backend refuses it for the same reason it refuses ``dev``."""
    bundle = bundle_at(
        tmp_path,
        dev=None,
        trust="first-party",
        python={"package": "acme-x", "entry": "acme-x", "mcp": {"transport": "stdio"}},
    )
    raw = json.loads((bundle / "manifest.json").read_text())
    raw.pop("dev")
    (bundle / "manifest.json").write_text(json.dumps(raw))
    with pytest.raises(InstallError, match="more than one principal"):
        await installer(tmp_path, DeploymentProfile.SHARED).install(bundle)


@pytest.mark.asyncio
async def test_the_refusal_names_the_packaging_that_would_work(tmp_path):
    """An administrator reading this needs the fix, not the rule."""
    with pytest.raises(InstallError, match="'endpoint' packaging"):
        await installer(tmp_path, DeploymentProfile.SHARED).install(bundle_at(tmp_path))


@pytest.mark.asyncio
async def test_pages_are_refused_on_a_shared_backend(tmp_path):
    """A frame is hosted by the igni app beside the user, and a
    shared backend has no such app — the pages would be served and
    never shown."""
    bundle = bundle_at(
        tmp_path,
        dev=None,
        endpoint={"url": "https://scanner.internal/mcp"},
        permissions={"egress": ["scanner.internal"], "rpc": ["ext.acme.scanner.*"]},
    )
    raw = json.loads((bundle / "manifest.json").read_text())
    del raw["dev"]
    (bundle / "manifest.json").write_text(json.dumps(raw))

    with pytest.raises(InstallError, match="no such app"):
        await installer(tmp_path, DeploymentProfile.SHARED).install(bundle)


@pytest.mark.asyncio
async def test_the_same_bundle_installs_on_a_single_user_backend(tmp_path):
    """The rule is about the deployment, not about the extension."""
    entry = await installer(tmp_path, DeploymentProfile.SINGLE_USER).install(bundle_at(tmp_path))
    assert entry.state is ExtensionState.READY, entry.reason


# ── Grants that could not be delivered ─────────────────────────────


@pytest.mark.asyncio
async def test_a_workspace_grant_on_an_endpoint_is_refused(tmp_path):
    """Declared, approved by an administrator, and silently nothing.

    igni delivers workspace and secrets in the environment of a
    process it starts. An endpoint is somebody else's process, so the
    grant never arrives — which is the exact failure this system has
    spent its time removing.
    """
    bundle = bundle_at(tmp_path)
    raw = json.loads((bundle / "manifest.json").read_text())
    del raw["dev"]
    raw["endpoint"] = {"url": "https://scanner.internal/mcp"}
    raw["permissions"] = {"egress": ["scanner.internal"], "workspace": "write"}
    raw["contributes"] = {}
    (bundle / "manifest.json").write_text(json.dumps(raw))

    with pytest.raises(InstallError, match="never arrive"):
        await installer(tmp_path, DeploymentProfile.SINGLE_USER).install(bundle)


@pytest.mark.asyncio
async def test_a_secret_grant_on_an_endpoint_is_refused(tmp_path):
    bundle = bundle_at(tmp_path)
    raw = json.loads((bundle / "manifest.json").read_text())
    del raw["dev"]
    raw["endpoint"] = {"url": "https://scanner.internal/mcp"}
    raw["permissions"] = {
        "egress": ["scanner.internal"],
        "secrets": ["ledger-token"],
    }
    raw["contributes"] = {}
    (bundle / "manifest.json").write_text(json.dumps(raw))

    with pytest.raises(InstallError, match="ledger-token"):
        await installer(tmp_path, DeploymentProfile.SINGLE_USER).install(bundle)


@pytest.mark.asyncio
async def test_those_grants_are_fine_on_a_process_igni_starts(tmp_path):
    """Because there, igni sets the environment."""
    bundle = bundle_at(tmp_path)
    raw = json.loads((bundle / "manifest.json").read_text())
    raw["permissions"] = {
        **raw.get("permissions", {}),
        "workspace": "read",
    }
    (bundle / "manifest.json").write_text(json.dumps(raw))

    entry = await installer(tmp_path, DeploymentProfile.SINGLE_USER).install(bundle)
    assert entry.state is ExtensionState.READY, entry.reason

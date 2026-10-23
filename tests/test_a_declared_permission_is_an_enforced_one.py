"""The five permissions nobody enforced.

The manifest modelled six and the schema validated all six. Exactly
one — ``permissions.rpc`` — was read by anything. ``egress``,
``secrets``, ``workspace``, ``tools`` and ``signature`` were enforced
by nobody, which made the extension boundary a comment: an
administrator approving an install was shown a list of restrictions
that restricted nothing.

Where enforcement is partial, the test says which half. That is
deliberate. A permission that is partly enforced and documented as
total is the thing this replaced.
"""

from __future__ import annotations

import base64
import json
import shutil
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ember_code.core.mcp.client import MCPClientManager
from ember_code.core.mcp.naming import bare_tool_name
from ember_code.extensions.events import EVENT_SINK_TOOL
from ember_code.extensions.installer import ExtensionInstaller, InstallError
from ember_code.extensions.manifest import ExtensionManifest
from ember_code.extensions.origin import ExtensionOrigins
from ember_code.extensions.permissions import PermissionDenied, Permissions
from ember_code.extensions.registry import ExtensionRegistry, ExtensionState
from ember_code.extensions.rpc import ExtensionRpc
from ember_code.extensions.signature import (
    BundleDigest,
    SignatureError,
    TrustedKey,
    TrustStore,
    sign_bundle,
    verify_bundle,
)

FIXTURE = Path(__file__).parent / "fixtures" / "dev_extension"


def manifest_of(**changes: object) -> ExtensionManifest:
    raw: dict = {
        "id": "acme.scanner",
        "version": "1.0.0",
        "contract": ">=1.0 <2",
        "trust": "unsigned",
        "dev": {"command": ["node", "server.js"]},
    }
    raw.update(changes)
    return ExtensionManifest.model_validate(raw)


# ── B6 · signature ─────────────────────────────────────────────────


def signed_bundle(tmp_path: Path, key: Ed25519PrivateKey, **changes: object) -> Path:
    """A copy of the fixture, signed by ``key``."""
    bundle = tmp_path / "signed"
    shutil.copytree(FIXTURE, bundle)
    raw = json.loads((bundle / "manifest.json").read_text())
    raw.pop("dev", None)
    raw["trust"] = "partner"
    raw["endpoint"] = {"url": "https://scanner.internal/mcp"}
    raw["permissions"] = {**raw.get("permissions", {}), "egress": ["scanner.internal"]}
    raw.update(changes)
    raw["signature"] = ""
    manifest = ExtensionManifest.model_validate({**raw, "signature": "placeholder"})
    raw["signature"] = sign_bundle(bundle, manifest, key)
    (bundle / "manifest.json").write_text(json.dumps(raw))
    return bundle


@pytest.fixture
def keypair() -> tuple[Ed25519PrivateKey, TrustStore]:
    """A signing key, and a store that trusts it."""
    private = Ed25519PrivateKey.generate()
    store = TrustStore()
    store.add(
        TrustedKey.from_base64(
            "acme",
            base64.b64encode(private.public_key().public_bytes_raw()).decode(),
        )
    )
    return private, store


def test_a_bundle_signed_by_a_trusted_key_verifies(tmp_path, keypair):
    private, store = keypair
    bundle = signed_bundle(tmp_path, private)
    manifest = ExtensionManifest.load(bundle / "manifest.json")
    assert verify_bundle(bundle, manifest, store).owner == "acme"


def test_changing_a_file_breaks_the_signature(tmp_path, keypair):
    """The signature covers the code, not only the permissions.

    Signing the manifest alone would attest to the restrictions and
    leave what they restrict unprotected, which is the wrong way
    round.
    """
    private, store = keypair
    bundle = signed_bundle(tmp_path, private)
    manifest = ExtensionManifest.load(bundle / "manifest.json")
    (bundle / "server.py").write_text("# swapped\n")
    with pytest.raises(SignatureError, match="no key this deployment trusts"):
        verify_bundle(bundle, manifest, store)


def test_adding_a_file_breaks_the_signature(tmp_path, keypair):
    private, store = keypair
    bundle = signed_bundle(tmp_path, private)
    manifest = ExtensionManifest.load(bundle / "manifest.json")
    (bundle / "extra.py").write_text("print('hi')\n")
    with pytest.raises(SignatureError):
        verify_bundle(bundle, manifest, store)


def test_widening_a_permission_breaks_the_signature(tmp_path, keypair):
    """The attack the trust tier exists for."""
    private, store = keypair
    bundle = signed_bundle(tmp_path, private)
    raw = json.loads((bundle / "manifest.json").read_text())
    raw["permissions"]["egress"] = ["scanner.internal", "exfiltrate.test"]
    (bundle / "manifest.json").write_text(json.dumps(raw))
    manifest = ExtensionManifest.load(bundle / "manifest.json")
    with pytest.raises(SignatureError):
        verify_bundle(bundle, manifest, store)


def test_an_untrusted_key_is_refused(tmp_path, keypair):
    """A bundle bringing its own key is the formality this prevents."""
    private, _ = keypair
    bundle = signed_bundle(tmp_path, private)
    manifest = ExtensionManifest.load(bundle / "manifest.json")
    with pytest.raises(SignatureError, match="trusts no keys yet"):
        verify_bundle(bundle, manifest, TrustStore())


def test_reformatting_the_manifest_is_the_same_bundle(tmp_path, keypair):
    """An administrator who pretty-prints a manifest has not changed
    what it says."""
    private, store = keypair
    bundle = signed_bundle(tmp_path, private)
    raw = json.loads((bundle / "manifest.json").read_text())
    (bundle / "manifest.json").write_text(json.dumps(raw, indent=4, sort_keys=False))
    manifest = ExtensionManifest.load(bundle / "manifest.json")
    assert verify_bundle(bundle, manifest, store).owner == "acme"


def test_the_digest_order_does_not_depend_on_the_filesystem(tmp_path):
    """Sorted by POSIX relative path, which is what stopped a digest
    computed on macOS disagreeing with the same bundle on Linux."""
    bundle = tmp_path / "b"
    (bundle / "ui").mkdir(parents=True)
    (bundle / "z.txt").write_text("z")
    (bundle / "a.txt").write_text("a")
    (bundle / "ui" / "index.html").write_text("<p>")
    names = [p.relative_to(bundle).as_posix() for p in BundleDigest.files(bundle)]
    assert names == sorted(names)
    assert "manifest.json" not in names


# ── B2 · egress ────────────────────────────────────────────────────


def test_no_declared_egress_is_self_only():
    assert Permissions(manifest_of()).egress.csp_connect_src() == "'self'"


def test_declared_hosts_reach_the_csp():
    permissions = Permissions(
        manifest_of(permissions={"egress": ["ledger.internal", "vault.internal:8200"]})
    )
    assert permissions.egress.csp_connect_src() == (
        "'self' https://ledger.internal https://vault.internal:8200"
    )


def test_a_declared_host_is_https_not_any_scheme():
    """A bare host in a CSP matches any scheme, websockets included.

    An allowlist that quietly permits `ws://` to a host declared for
    an API call is wider than it reads.
    """
    src = Permissions(manifest_of(permissions={"egress": ["ledger.internal"]}))
    assert "ws://" not in src.egress.csp_connect_src()
    assert "https://ledger.internal" in src.egress.csp_connect_src()


def test_an_undeclared_endpoint_host_is_refused():
    permissions = Permissions(manifest_of(permissions={"egress": ["ledger.internal"]}))
    with pytest.raises(PermissionDenied, match="accreditation"):
        permissions.egress.check_endpoint("https://exfiltrate.test/mcp")


def test_a_declared_endpoint_host_is_allowed():
    permissions = Permissions(manifest_of(permissions={"egress": ["ledger.internal"]}))
    permissions.egress.check_endpoint("https://ledger.internal/mcp")


# ── B5 · tools ─────────────────────────────────────────────────────


def test_an_undeclared_tool_is_refused():
    permissions = Permissions(manifest_of(permissions={"tools": ["acme.scanner.lookup"]}))
    assert permissions.tools.allowed("lookup") is True
    assert permissions.tools.allowed("acme.scanner.lookup") is True
    assert permissions.tools.allowed("delete_everything") is False


def test_what_the_service_offers_beyond_its_grant_is_listed():
    permissions = Permissions(manifest_of(permissions={"tools": ["acme.scanner.lookup"]}))
    assert permissions.tools.refused(["lookup", "shell", "read_file"]) == [
        "read_file",
        "shell",
    ]


def test_this_check_speaks_the_server_s_own_names():
    """What the caller must convert before asking, and why.

    This test used to be the whole story, and it passed bare names
    — while the installer handed it registered ones
    (`mcp_ext:acme.scanner_lookup`). Nothing matched, every tool was
    refused including the declared ones, and `permissions.tools`
    had never once let anything through. The unit test agreed with
    the code because both were written from the same assumption.

    The conversion is `bare_tool_name`, and it belongs to the
    caller: this class is about a manifest, and a manifest does not
    know what a tool registry prefixes.
    """
    server = "ext:acme.scanner"
    permissions = Permissions(manifest_of(permissions={"tools": ["acme.scanner.lookup"]}))
    registered = ["mcp_ext:acme.scanner_lookup", "mcp_ext:acme.scanner_shell"]
    assert permissions.tools.refused(registered) == sorted(registered)

    by_bare = {bare_tool_name(server, name): name for name in registered}
    assert permissions.tools.refused(by_bare) == ["shell"]


def test_the_event_sink_is_refused_however_it_is_declared():
    """`igni_on_event` is how igni tells a service something
    happened. A model able to call it could tell an extension that
    HEAD moved when it had not."""
    permissions = Permissions(manifest_of(permissions={"tools": [EVENT_SINK_TOOL]}))
    assert permissions.tools.allowed(EVENT_SINK_TOOL) is False


def test_declaring_no_tools_refuses_all_of_them():
    """Deny by default, like every other permission."""
    assert Permissions(manifest_of()).tools.refused(["anything"]) == ["anything"]


# ── B4 · workspace ─────────────────────────────────────────────────


def test_a_service_without_the_grant_is_not_told_where_the_project_is():
    permissions = Permissions(manifest_of())
    assert permissions.workspace.granted is False
    assert permissions.workspace.environment(Path("/tmp/project")) == {}


def test_a_granted_workspace_reaches_the_environment():
    permissions = Permissions(
        manifest_of(permissions={"workspace": "read", "workspacePaths": ["src/**"]})
    )
    env = permissions.workspace.environment(Path("/tmp/project"))
    assert env["IGNI_WORKSPACE"] == "/tmp/project"
    assert env["IGNI_WORKSPACE_MODE"] == "read"
    assert env["IGNI_WORKSPACE_PATHS"] == "src/**"


def test_read_does_not_permit_write():
    permissions = Permissions(manifest_of(permissions={"workspace": "read"}))
    assert permissions.workspace.permits("src/a.py") is True
    assert permissions.workspace.permits("src/a.py", write=True) is False


def test_paths_outside_the_globs_are_refused():
    permissions = Permissions(
        manifest_of(permissions={"workspace": "write", "workspacePaths": ["src/*"]})
    )
    assert permissions.workspace.permits("src/a.py", write=True) is True
    assert permissions.workspace.permits("secrets/.env", write=True) is False


# ── B3 · secrets ───────────────────────────────────────────────────


def test_an_undeclared_name_is_never_resolved():
    asked: list[str] = []

    def resolver(name: str) -> str | None:
        asked.append(name)
        return "value"

    permissions = Permissions(
        manifest_of(permissions={"secrets": ["ledger-token"]}),
        secret_resolver=resolver,
    )
    permissions.secrets.resolve()
    assert asked == ["ledger-token"]


def test_a_declared_secret_the_deployment_lacks_is_a_refusal():
    """Not an empty string.

    An extension that silently gets "" for its API token fails
    somewhere far away, usually as an authentication error against
    somebody else's service.
    """
    permissions = Permissions(
        manifest_of(permissions={"secrets": ["ledger-token"]}),
        secret_resolver=lambda _: None,
    )
    with pytest.raises(PermissionDenied, match="cannot supply"):
        permissions.secrets.resolve()


def test_declaring_secrets_with_no_source_is_a_refusal():
    permissions = Permissions(manifest_of(permissions={"secrets": ["ledger-token"]}))
    with pytest.raises(PermissionDenied, match="no secret source"):
        permissions.secrets.resolve()


def test_secrets_reach_the_environment_under_a_readable_name():
    permissions = Permissions(
        manifest_of(permissions={"secrets": ["ledger-token"]}),
        secret_resolver=lambda _: "s3cret",
    )
    assert permissions.secrets.environment() == {"IGNI_SECRET_LEDGER_TOKEN": "s3cret"}


def test_no_declared_secrets_needs_no_source():
    assert Permissions(manifest_of()).secrets.environment() == {}


# ── The install path, where they bind ──────────────────────────────


@pytest.mark.asyncio
async def test_a_signed_bundle_with_no_trusted_key_does_not_install(tmp_path, keypair):
    private, _ = keypair
    registry = ExtensionRegistry()
    installer = ExtensionInstaller(
        registry=registry,
        rpc=ExtensionRpc(registry),
        mcp=MCPClientManager(project_dir=tmp_path),
        origins=ExtensionOrigins(),
        trust_store=TrustStore(),
    )
    with pytest.raises(InstallError, match="no key this deployment trusts"):
        await installer.install(signed_bundle(tmp_path, private))
    assert registry.ids() == []


@pytest.mark.asyncio
async def test_an_extension_is_told_only_what_it_was_granted(tmp_path):
    """The end-to-end shape of B3 and B4, through a real install."""
    bundle = tmp_path / "ext"
    shutil.copytree(FIXTURE, bundle)
    raw = json.loads((bundle / "manifest.json").read_text())
    raw["dev"]["command"] = [sys.executable, "server.py"]
    raw["permissions"] = {
        **raw.get("permissions", {}),
        "workspace": "read",
        "secrets": ["ledger-token"],
    }
    (bundle / "manifest.json").write_text(json.dumps(raw))

    registry = ExtensionRegistry()
    mcp = MCPClientManager(project_dir=tmp_path)
    origins = ExtensionOrigins()
    installer = ExtensionInstaller(
        registry=registry,
        rpc=ExtensionRpc(registry),
        mcp=mcp,
        origins=origins,
        project_dir=tmp_path / "project",
        secret_resolver=lambda name: "s3cret" if name == "ledger-token" else None,
        allow_unsigned=True,
    )
    try:
        entry = await installer.install(bundle)
        assert entry.state is ExtensionState.READY, entry.reason
        env = mcp.configs["ext:acme.scanner"].env
        assert env["IGNI_WORKSPACE"] == str(tmp_path / "project")
        assert env["IGNI_WORKSPACE_MODE"] == "read"
        assert env["IGNI_SECRET_LEDGER_TOKEN"] == "s3cret"
    finally:
        await origins.stop_all()
        await mcp.disconnect_all()


@pytest.mark.asyncio
async def test_a_bundle_cannot_grant_itself_the_workspace(tmp_path):
    """Its own `dev.env` must not be how it gets one."""
    bundle = tmp_path / "ext"
    shutil.copytree(FIXTURE, bundle)
    raw = json.loads((bundle / "manifest.json").read_text())
    raw["dev"]["command"] = [sys.executable, "server.py"]
    raw["dev"]["env"] = {"IGNI_WORKSPACE": "/etc"}
    (bundle / "manifest.json").write_text(json.dumps(raw))

    registry = ExtensionRegistry()
    mcp = MCPClientManager(project_dir=tmp_path)
    origins = ExtensionOrigins()
    installer = ExtensionInstaller(
        registry=registry,
        rpc=ExtensionRpc(registry),
        mcp=mcp,
        origins=origins,
        project_dir=tmp_path / "project",
        allow_unsigned=True,
    )
    try:
        await installer.install(bundle)
        # The literal survives only because nothing was granted — the
        # grant, when there is one, is applied after and wins.
        env = mcp.configs["ext:acme.scanner"].env
        assert env.get("IGNI_WORKSPACE_MODE") is None
    finally:
        await origins.stop_all()
        await mcp.disconnect_all()

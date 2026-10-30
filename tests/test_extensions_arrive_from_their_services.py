"""Extensions arrive from their services, through igni server.

igni server's ``docs/service-identity.md`` §8. A service uploads its bundle signed
with the key it registered; once a super admin approves the service,
igni lists it to every signed-in machine, and the host keeps what is
installed in step: download what differs, check the signature against
the publisher's key and the digest against the bytes, install through
the ordinary boot path, remove what is no longer listed.

Each refusal below is a way a bundle could be other than what the
service signed, and each one must leave the machine as it was.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from ember_code.extensions.bundle_signature import BundleSignatureError, PublisherKeys
from ember_code.extensions.distribution import ExtensionDistribution
from ember_code.extensions.installer import ExtensionInstaller, InstallError
from ember_code.extensions.launcher import ExtensionLauncher
from ember_code.extensions.manifest import ExtensionManifest
from ember_code.extensions.registry import ExtensionRegistry, ExtensionState
from ember_code.extensions.rpc import ExtensionRpc
from ember_code.extensions.service_token import IgniSession
from ember_code.extensions.store import DistributionRecord, ExtensionStore
from tests.extension_distribution_helpers import (
    API_URL,
    SESSION,
    FakeIgni,
    Listed,
    Publisher,
    b64url,
    bundle_archive,
    manifest,
)

# ── The signature ──────────────────────────────────────────────────


@pytest.fixture(scope="module")
def rsa_publisher() -> Publisher:
    return Publisher("RSA")


@pytest.fixture(scope="module")
def ec_publisher() -> Publisher:
    return Publisher("EC", kid="ec-key")


CLAIM = {"extension": "acme.notes", "version": "1.0.0", "sha256": "ab" * 32}


@pytest.mark.parametrize("which", ["rsa_publisher", "ec_publisher"])
def test_a_signature_by_the_publisher_verifies(which, request):
    publisher: Publisher = request.getfixturevalue(which)
    claim = PublisherKeys(publisher.jwks()).verify(publisher.sign(CLAIM))
    assert (claim.extension, claim.version, claim.sha256) == tuple(CLAIM.values())


def test_another_key_is_refused(rsa_publisher):
    stranger = Publisher("RSA", kid=rsa_publisher.kid)
    with pytest.raises(BundleSignatureError, match="not signed by its publisher"):
        PublisherKeys(rsa_publisher.jwks()).verify(stranger.sign(CLAIM))


def test_a_changed_claim_is_refused(rsa_publisher):
    head, _, signature = rsa_publisher.sign(CLAIM).split(".")
    forged = b64url(json.dumps({**CLAIM, "version": "9.9.9"}).encode())
    with pytest.raises(BundleSignatureError):
        PublisherKeys(rsa_publisher.jwks()).verify(f"{head}.{forged}.{signature}")


@pytest.mark.parametrize("alg", ["none", "HS256", "PS256"])
def test_only_rs256_and_es256_are_accepted(rsa_publisher, alg):
    token = rsa_publisher.sign(CLAIM, header={"alg": alg, "kid": rsa_publisher.kid})
    with pytest.raises(BundleSignatureError, match="only RS256 and ES256"):
        PublisherKeys(rsa_publisher.jwks()).verify(token)


def test_the_algorithm_must_match_the_key(ec_publisher, rsa_publisher):
    """The token does not get to pick: an RS256 header against an EC
    key has no key to be checked with."""
    token = rsa_publisher.sign(CLAIM, header={"alg": "RS256", "kid": ec_publisher.kid})
    with pytest.raises(BundleSignatureError):
        PublisherKeys(ec_publisher.jwks()).verify(token)


def test_a_kid_the_publisher_does_not_have_is_refused(rsa_publisher):
    token = rsa_publisher.sign(CLAIM, header={"alg": "RS256", "kid": "somebody-else"})
    with pytest.raises(BundleSignatureError):
        PublisherKeys(rsa_publisher.jwks()).verify(token)


def test_a_publisher_with_no_keys_verifies_nothing(rsa_publisher):
    with pytest.raises(BundleSignatureError, match="no keys"):
        PublisherKeys({"keys": []}).verify(rsa_publisher.sign(CLAIM))


# ── The sync ───────────────────────────────────────────────────────


class Recorded:
    """The lifecycle, as calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def activate(self, extension_id: str, bundle: Path) -> None:
        self.calls.append(("activate", extension_id))
        assert (bundle / "manifest.json").is_file()

    async def deactivate(self, extension_id: str) -> None:
        self.calls.append(("deactivate", extension_id))


class World:
    def __init__(self, tmp_path: Path, *, signed_in: bool = True) -> None:
        self.igni = FakeIgni()
        self.publisher = Publisher("RSA")
        self.store = ExtensionStore(tmp_path / "store")
        self.lifecycle = Recorded()
        self.signed_in = signed_in
        self.priority_changed = 0
        self.distribution = ExtensionDistribution(
            self.store,
            api_url=lambda: API_URL,
            session=self.session,
            lifecycle=self.lifecycle,
            transport=self.igni.transport(),
            on_priority=self._priority_changed,
        )

    def _priority_changed(self) -> None:
        self.priority_changed += 1

    def session(self) -> IgniSession | None:
        return IgniSession(access_token=SESSION) if self.signed_in else None

    def publish(self, raw: dict[str, Any] | None = None, **listed: Any) -> Listed:
        raw = raw if raw is not None else manifest()
        entry = Listed(
            bundle_archive(raw, {"wheels/placeholder.txt": b"x"}),
            self.publisher,
            extension_id=listed.pop("extension_id", raw["id"]),
            version=listed.pop("version", raw["version"]),
            **listed,
        )
        self.igni.publish(entry)
        return entry


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


async def test_a_listed_extension_is_installed_with_its_record(world):
    entry = world.publish()
    report = await world.distribution.sync()

    assert report.installed == ["acme.notes"], report
    assert (world.store.path_for("acme.notes") / "manifest.json").is_file()
    record = world.store.distribution_of("acme.notes")
    assert record is not None
    assert record.service_url == "https://notes.example.com"
    assert record.sha256 == entry.listed_sha256
    assert world.lifecycle.calls == [("deactivate", "acme.notes"), ("activate", "acme.notes")]


async def test_the_priority_order_is_kept_and_announced_once(world):
    """Kept, so the next start settles collisions before igni is asked
    anything; announced only when it changed, so a sync does not rebuild
    every agent for nothing."""
    world.publish()
    world.igni.priority = ["acme.notes", "acme.review"]

    report = await world.distribution.sync()
    assert report.priority_changed
    assert world.store.priority() == ["acme.notes", "acme.review"]
    assert world.priority_changed == 1

    again = await world.distribution.sync()
    assert not again.priority_changed
    assert world.priority_changed == 1

    world.igni.priority = ["acme.review", "acme.notes"]
    await world.distribution.sync()
    assert world.store.priority() == ["acme.review", "acme.notes"]
    assert world.priority_changed == 2


async def test_a_failed_listing_keeps_the_priority_order(world):
    world.igni.priority = ["a.b", "c.d"]
    await world.distribution.sync()
    world.igni.listing_status = 503
    await world.distribution.sync()
    assert world.store.priority() == ["a.b", "c.d"]


async def test_the_listing_is_asked_for_as_the_signed_in_person(world):
    world.publish()
    await world.distribution.sync()
    assert {r.headers["authorization"] for r in world.igni.requests} == {f"Bearer {SESSION}"}


async def test_an_unchanged_extension_is_not_downloaded_again(world):
    world.publish()
    await world.distribution.sync()
    world.lifecycle.calls.clear()

    report = await world.distribution.sync()
    assert report.unchanged == ["acme.notes"]
    assert len(world.igni.downloads()) == 1
    assert world.lifecycle.calls == []


async def test_a_new_version_replaces_the_old(world):
    world.publish()
    await world.distribution.sync()
    world.publish(manifest(version="1.1.0"))

    report = await world.distribution.sync()
    assert report.installed == ["acme.notes"]
    assert world.store.read(world.store.path_for("acme.notes")).version == "1.1.0"
    assert world.store.distribution_of("acme.notes").version == "1.1.0"


async def test_new_bytes_under_the_same_version_are_fetched(world):
    """A service may upload again without bumping its version; the
    digest is what says the bundle changed."""
    world.publish()
    await world.distribution.sync()
    world.publish(manifest(description="rebuilt"))
    report = await world.distribution.sync()
    assert report.installed == ["acme.notes"]
    assert len(world.igni.downloads()) == 2


# ── Refusals: nothing is stored, nothing is started ────────────────


def _nothing_happened(world: World) -> None:
    assert not world.store.path_for("acme.notes").exists()
    assert world.store.distribution_of("acme.notes") is None
    assert world.lifecycle.calls == []


async def test_a_signature_by_another_key_is_refused(world):
    raw = manifest()
    archive = bundle_archive(raw)
    impostor = Publisher("RSA", kid=world.publisher.kid)
    world.igni.publish(
        Listed(
            archive,
            world.publisher,
            signature=impostor.sign_bundle("acme.notes", "1.0.0", archive),
        )
    )
    report = await world.distribution.sync()
    assert "not signed by its publisher" in report.failed["acme.notes"]
    _nothing_happened(world)


async def test_an_archive_that_is_not_the_listed_one_is_refused(world):
    world.publish(listed_sha256="0" * 64)
    report = await world.distribution.sync()
    assert "not the one the listing describes" in report.failed["acme.notes"]
    _nothing_happened(world)


async def test_an_archive_that_does_not_match_its_digest_header_is_refused(world):
    world.publish(header_sha256="f" * 64)
    report = await world.distribution.sync()
    assert "digest igni sent" in report.failed["acme.notes"]
    _nothing_happened(world)


async def test_a_signature_over_other_bytes_is_refused(world):
    """Signed by the right key — for a different archive."""
    archive = bundle_archive(manifest())
    other = bundle_archive(manifest(description="the one that was signed"))
    world.igni.publish(
        Listed(
            archive,
            world.publisher,
            signature=world.publisher.sign_bundle("acme.notes", "1.0.0", other),
        )
    )
    report = await world.distribution.sync()
    assert "different bytes" in report.failed["acme.notes"]
    _nothing_happened(world)


async def test_a_manifest_that_is_not_what_was_signed_is_refused(world):
    """The claim and the listing say 1.0.0; the manifest inside says
    otherwise. Every name for the bundle must agree."""
    archive = bundle_archive(manifest(version="2.0.0"))
    world.igni.publish(Listed(archive, world.publisher, version="1.0.0"))
    report = await world.distribution.sync()
    assert "manifest is acme.notes 2.0.0" in report.failed["acme.notes"]
    _nothing_happened(world)


async def test_a_claim_for_another_extension_is_refused(world):
    archive = bundle_archive(manifest())
    world.igni.publish(
        Listed(
            archive,
            world.publisher,
            signature=world.publisher.sign_bundle("acme.other", "1.0.0", archive),
        )
    )
    report = await world.distribution.sync()
    assert "signature is for acme.other" in report.failed["acme.notes"]
    _nothing_happened(world)


async def test_the_trust_label_does_not_decide(world):
    """Artifacts ships ``python`` packaging with ``trust: unsigned`` and
    no signature field. Provenance is the JWS igni handed over, so the
    label an author wrote is not a reason to refuse it."""
    world.publish(manifest(trust="unsigned"))
    report = await world.distribution.sync()
    assert report.installed == ["acme.notes"], report


async def test_a_dev_command_is_not_installed_from_a_service(world):
    raw = manifest(trust="unsigned", dev={"command": ["sh", "-c", "anything"]})
    raw.pop("python")
    world.publish(raw)
    report = await world.distribution.sync()
    assert "'dev' packaging" in report.failed["acme.notes"]
    _nothing_happened(world)


async def test_an_archive_reaching_outside_itself_is_refused(world):
    escape = tarfile.TarInfo("../outside.txt")
    escape.size = 4
    archive = bundle_archive(manifest(), extra_members=[(escape, b"evil")])
    world.igni.publish(Listed(archive, world.publisher))
    report = await world.distribution.sync()
    assert "could not be unpacked" in report.failed["acme.notes"]
    _nothing_happened(world)


async def test_a_link_out_of_the_archive_is_refused(world):
    link = tarfile.TarInfo("ssh")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"
    archive = bundle_archive(manifest(), extra_members=[(link, None)])
    world.igni.publish(Listed(archive, world.publisher))
    report = await world.distribution.sync()
    assert "acme.notes" in report.failed
    _nothing_happened(world)


async def test_a_bad_bundle_does_not_stop_the_others(world):
    world.publish(manifest("acme.good"), extension_id="acme.good")
    world.publish(manifest("acme.bad"), extension_id="acme.bad", listed_sha256="0" * 64)
    report = await world.distribution.sync()
    assert report.installed == ["acme.good"]
    assert set(report.failed) == {"acme.bad"}


async def test_a_refused_upgrade_keeps_the_installed_version_running(world):
    world.publish()
    await world.distribution.sync()
    world.lifecycle.calls.clear()
    world.publish(manifest(version="1.1.0"), listed_sha256="0" * 64)

    report = await world.distribution.sync()
    assert "acme.notes" in report.failed
    assert world.store.distribution_of("acme.notes").version == "1.0.0"
    assert world.lifecycle.calls == [], "the running version must not be stopped"


# ── Removal ────────────────────────────────────────────────────────


async def test_an_extension_whose_service_is_unlisted_is_removed(world):
    world.publish()
    await world.distribution.sync()
    world.lifecycle.calls.clear()
    world.igni.withdraw("acme.notes")

    report = await world.distribution.sync()
    assert report.removed == ["acme.notes"]
    assert world.lifecycle.calls == [("deactivate", "acme.notes")]
    assert not world.store.path_for("acme.notes").exists()
    assert world.store.distribution_of("acme.notes") is None


async def test_a_failed_listing_removes_nothing(world):
    """ "igni did not answer" is not "the service is gone"."""
    world.publish()
    await world.distribution.sync()
    world.igni.listing_status = 503

    report = await world.distribution.sync()
    assert report.skipped and "503" in report.skipped
    assert world.store.path_for("acme.notes").is_dir()


async def test_an_unreachable_igni_removes_nothing(tmp_path):
    w = World(tmp_path)
    w.publish()
    await w.distribution.sync()

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to igni", request=request)

    w.distribution._transport = httpx.MockTransport(unreachable)
    report = await w.distribution.sync()
    assert report.skipped and "could not be reached" in report.skipped
    assert w.store.path_for("acme.notes").is_dir()


# ── Development installs are left alone ────────────────────────────


def _development_install(store: ExtensionStore, tmp_path: Path, extension_id: str) -> None:
    source = tmp_path / "source" / extension_id
    source.mkdir(parents=True)
    raw = {
        "id": extension_id,
        "version": "0.0.1",
        "contract": ">=1.0 <2",
        "trust": "unsigned",
        "dev": {"command": ["python", "server.py"]},
    }
    (source / "manifest.json").write_text(json.dumps(raw))
    store.add(source)


async def test_a_development_install_of_the_same_id_is_not_overwritten(world, tmp_path):
    _development_install(world.store, tmp_path, "acme.notes")
    world.publish()

    report = await world.distribution.sync()
    assert report.development == ["acme.notes"]
    assert world.igni.downloads() == []
    assert world.store.read(world.store.path_for("acme.notes")).version == "0.0.1"


async def test_a_development_install_is_not_removed_for_being_unlisted(world, tmp_path):
    _development_install(world.store, tmp_path, "acme.mine")
    report = await world.distribution.sync()
    assert report.removed == []
    assert world.store.path_for("acme.mine").is_dir()


# ── When there is nobody to ask for ────────────────────────────────


async def test_nothing_is_asked_when_nobody_is_signed_in(tmp_path):
    w = World(tmp_path, signed_in=False)
    w.publish()
    report = await w.distribution.sync()
    assert report.skipped == "nobody is signed in to igni"
    assert w.igni.requests == []


async def test_nothing_is_asked_when_no_server_is_configured(tmp_path):
    w = World(tmp_path)
    w.distribution._api_url = lambda: ""
    report = await w.distribution.sync()
    assert report.skipped == "no igni server is configured"
    assert w.igni.requests == []


async def test_a_scheduled_sync_runs_in_the_background(world):
    world.publish()
    task = world.distribution.schedule()
    report = await task
    assert report is not None and report.installed == ["acme.notes"]


# ── The installer's half: trust is where it came from ──────────────


class StubMcp:
    """Enough of the MCP manager for an install that starts nothing real."""

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


def _installer(registry: ExtensionRegistry, mcp: StubMcp, **kwargs: Any) -> ExtensionInstaller:
    return ExtensionInstaller(registry=registry, rpc=ExtensionRpc(registry), mcp=mcp, **kwargs)


def _endpoint_bundle(tmp_path: Path, **changes: Any) -> Path:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    raw = {
        "id": "acme.notes",
        "version": "1.0.0",
        "contract": ">=1.0 <2",
        "trust": "partner",
        "endpoint": {"url": "https://notes.internal/mcp"},
        "permissions": {"egress": ["notes.internal"]},
        **changes,
    }
    (bundle / "manifest.json").write_text(json.dumps(raw))
    return bundle


def _record(**changes: Any) -> DistributionRecord:
    return DistributionRecord(
        **{
            "extension_id": "acme.notes",
            "version": "1.0.0",
            "sha256": "a" * 64,
            "service_url": "https://notes.example.com",
            **changes,
        }
    )


async def test_a_distributed_bundle_installs_without_the_dev_flag(tmp_path):
    registry = ExtensionRegistry()
    entry = await _installer(registry, StubMcp()).install(
        _endpoint_bundle(tmp_path), distribution=_record()
    )
    assert entry.state is ExtensionState.READY, entry.reason


async def test_the_same_bundle_without_a_record_needs_the_flag(tmp_path):
    registry = ExtensionRegistry()
    with pytest.raises(InstallError, match="IGNI_DEV_EXTENSIONS"):
        await _installer(registry, StubMcp()).install(_endpoint_bundle(tmp_path))
    assert registry.ids() == []


async def test_a_bundle_that_is_not_what_was_recorded_is_refused(tmp_path):
    with pytest.raises(InstallError, match="not what igni distributed"):
        await _installer(ExtensionRegistry(), StubMcp()).install(
            _endpoint_bundle(tmp_path), distribution=_record(version="0.9.0")
        )


async def test_a_recorded_bundle_labelled_unsigned_installs(tmp_path):
    bundle = _endpoint_bundle(tmp_path, trust="unsigned")
    entry = await _installer(ExtensionRegistry(), StubMcp()).install(bundle, distribution=_record())
    assert entry.state is ExtensionState.READY, entry.reason


async def test_a_recorded_dev_command_is_refused(tmp_path):
    bundle = _endpoint_bundle(tmp_path, trust="unsigned", dev={"command": ["sh"]})
    raw = json.loads((bundle / "manifest.json").read_text())
    raw.pop("endpoint")
    (bundle / "manifest.json").write_text(json.dumps(raw))
    with pytest.raises(InstallError, match="'dev' packaging"):
        await _installer(ExtensionRegistry(), StubMcp(), allow_development=True).install(
            bundle, distribution=_record()
        )


async def test_the_artifacts_manifest_as_shipped_is_accepted(tmp_path):
    """The manifest igni__artifacts ships, as the contract shows it:
    ``python`` packaging, ``trust: unsigned``, no signature."""
    shipped = {
        "id": "igni.artifacts",
        "version": "0.1.0",
        "contract": ">=1.0 <2",
        "name": "Artifacts",
        "trust": "unsigned",
        "python": {
            "package": "igni_artifacts_extension",
            "entry": "igni-artifacts-extension",
            "wheels": "wheels",
            "mcp": {"transport": "stdio"},
        },
        "contributes": {"plugin": "plugin"},
    }
    manifest_model = ExtensionManifest.model_validate(shipped)
    assert manifest_model.packaging == "python"
    assert not manifest_model.is_development_command


async def test_a_sync_installs_through_the_ordinary_boot_path(tmp_path):
    """Sync → store → launcher → installer, with the record deciding
    trust: no dev flag anywhere, and the extension comes up READY.
    Then withdrawn, and gone from the registry too."""
    world = World(tmp_path)
    registry = ExtensionRegistry()
    mcp = StubMcp()
    launcher = ExtensionLauncher(
        store=world.store, installer=_installer(registry, mcp), registry=registry
    )
    world.distribution._lifecycle = launcher
    raw = {
        "id": "acme.notes",
        "version": "1.0.0",
        "contract": ">=1.0 <2",
        "trust": "partner",
        "endpoint": {"url": "https://notes.internal/mcp"},
        "permissions": {"egress": ["notes.internal"]},
    }
    world.publish(raw)

    report = await world.distribution.sync()
    assert report.installed == ["acme.notes"]
    entry = registry.get("acme.notes")
    assert entry is not None and entry.state is ExtensionState.READY, entry

    world.igni.withdraw("acme.notes")
    await world.distribution.sync()
    assert registry.get("acme.notes") is None
    assert "ext:acme.notes" not in mcp.configs


def test_the_archive_helper_round_trips():
    """Guards the helper the tests above lean on."""
    archive = bundle_archive(manifest(), {"a.txt": b"a"})
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        assert sorted(tar.getnames()) == ["a.txt", "manifest.json"]


# ── An upgrade that cannot be stored keeps the version that works ──


async def test_a_failed_store_keeps_and_restarts_the_old_version(world, monkeypatch):
    world.publish()
    await world.distribution.sync()
    world.lifecycle.calls.clear()
    world.publish(manifest(version="1.1.0"))

    real_rename = Path.rename

    def full_disk(self: Path, target: Any) -> Any:
        if self.name.endswith(".incoming"):
            raise OSError("disk full")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", full_disk)
    report = await world.distribution.sync()
    monkeypatch.undo()

    assert "disk full" in report.failed["acme.notes"]
    assert world.store.read(world.store.path_for("acme.notes")).version == "1.0.0"
    assert world.store.distribution_of("acme.notes").version == "1.0.0"
    assert world.lifecycle.calls == [("deactivate", "acme.notes"), ("activate", "acme.notes")]


def test_a_replace_that_fails_leaves_the_old_bundle(tmp_path, monkeypatch):
    store = ExtensionStore(tmp_path / "store")
    old = tmp_path / "old"
    old.mkdir()
    (old / "manifest.json").write_text(json.dumps(manifest()))
    store.add(old)
    new = tmp_path / "new"
    new.mkdir()
    (new / "manifest.json").write_text(json.dumps(manifest(version="2.0.0")))

    real_rename = Path.rename

    def refuse_incoming(self: Path, target: Any) -> Any:
        if self.name.endswith(".incoming"):
            raise OSError("disk full")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", refuse_incoming)
    with pytest.raises(OSError, match="disk full"):
        store.add(new, replace=True)
    monkeypatch.undo()

    assert store.read(store.path_for("acme.notes")).version == "1.0.0"
    assert [p.name for p in store.installed()] == ["acme.notes"]
    assert not any(p.name.startswith(".acme.notes") for p in store.root.iterdir())


# ── Only what an org admin switched on for the person's group ─────────


async def test_an_extension_on_for_another_group_is_not_installed(world):
    """igni server lists only what the person gets; the machine checks the same rule."""
    world.publish(groups=["g-finance"])

    report = await world.distribution.sync()

    assert report.installed == []
    assert world.store.path_for("acme.notes").exists() is False
    assert world.igni.downloads() == []


async def test_switched_off_for_their_group_it_is_removed(world):
    world.publish()
    await world.distribution.sync()
    assert world.store.distribution_of("acme.notes") is not None

    world.igni.listed["acme.notes"].groups = []
    report = await world.distribution.sync()

    assert report.removed == ["acme.notes"]
    assert world.store.distribution_of("acme.notes") is None


async def test_the_persons_group_is_kept(world):
    """What :class:`SignedInPerson` names on every call, before igni answers next time."""
    world.igni.group = "g-ops"

    await world.distribution.sync()

    stored = world.store.group()
    assert stored is not None and stored.group == "g-ops"


async def test_a_person_in_no_group_gets_nothing(world):
    world.publish()
    world.igni.group = None

    report = await world.distribution.sync()

    assert report.installed == []
    assert world.igni.downloads() == []

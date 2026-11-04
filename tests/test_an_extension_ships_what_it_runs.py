"""An extension ships what it runs that is not Python: its assets.

igni provides no database, JVM or model, and a machine with no network
must still install an extension that needs one. So the extension ships
them: declared in its manifest with their size and digest (inside the
signed bundle, so the signature covers them), held by igni server beside
the bundle, downloaded by a machine for its own platform only, checked
again, and laid out read-only under ``<bundle>/assets/<name>/`` for the
process — ``IGNI_EXTENSION_ASSETS``.

A distributed extension's assets come from igni and nowhere else. A
development install, which has no igni server behind it, fetches a
missing one from the ``source`` its manifest names, held to the same
digest.
"""

from __future__ import annotations

import io
import json
import os
import stat
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from ember_code.extensions.assets import (
    ASSETS_DIR_ENV,
    AssetCache,
    AssetError,
    AssetLayout,
    AssetPlatform,
    AssetSource,
    ReadOnlyTree,
)
from ember_code.extensions.installer import ExtensionInstaller
from ember_code.extensions.manifest import ExtensionManifest
from ember_code.extensions.python_env import PythonEnvironments
from ember_code.extensions.registry import ExtensionRegistry, ExtensionState
from ember_code.extensions.rpc import ExtensionRpc
from ember_code.extensions.store import DistributionRecord, ExtensionStore
from tests.extension_distribution_helpers import asset, manifest, tar_gz
from tests.test_a_python_extension_installs_offline import (
    Pretending,
    StubMcp,
    bundle_with_wheels,
)
from tests.test_extensions_arrive_from_their_services import World

HERE = AssetPlatform.current()

pytestmark = pytest.mark.skipif(HERE is None, reason="igni ships nothing for this platform")

JVM = tar_gz(
    {"jre/bin/java": b"#!/bin/sh\necho java\n", "jre/lib/rt.jar": b"jar"},
    executable=("jre/bin/java",),
)
MODEL = b"the same model everywhere"
OTHER = {p: f"a jre for {p}".encode() for p in AssetPlatform.specific() if p is not HERE}


def every_asset() -> list[dict[str, Any]]:
    """A JVM for each platform — this one's a real archive — and one model for all."""
    jres = [asset("jre", str(HERE), JVM, file="jre.tar.gz", unpack=True)]
    jres += [
        asset("jre", str(p), body, file="jre.tar.gz", unpack=True) for p, body in OTHER.items()
    ]
    return [*jres, asset("model", "any", MODEL, file="model.onnx")]


def declared(raw: dict[str, Any]) -> list:
    return ExtensionManifest.model_validate(manifest(assets=[raw])).assets


class TestTheManifest:
    def test_one_for_every_platform_or_one_for_each(self):
        ExtensionManifest.model_validate(manifest(assets=every_asset()))

    def test_a_platform_left_out_is_refused(self):
        partial = [a for a in every_asset() if a["name"] == "jre"][:2]
        with pytest.raises(ValidationError, match="one for each of"):
            ExtensionManifest.model_validate(manifest(assets=partial))

    def test_a_machine_takes_its_own_and_the_shared_ones(self):
        loaded = ExtensionManifest.model_validate(manifest(assets=every_asset()))
        from_here = [a for a in loaded.assets if a.platform in (HERE, AssetPlatform.ANY)]
        assert {(a.name, a.platform) for a in from_here} == {
            ("jre", HERE),
            ("model", AssetPlatform.ANY),
        }


def cached(tmp_path: Path, *bodies: bytes) -> AssetCache:
    cache = AssetCache(tmp_path / "cache")
    cache.root.mkdir(parents=True, exist_ok=True)
    for body in bodies:
        partial = cache.partial()
        partial.write_bytes(body)
        cache.adopt(partial, declared(asset("x", "any", body))[0])
    return cache


class TestLayingOut:
    def test_an_archive_is_unpacked_read_only_and_runnable(self, tmp_path):
        [jre] = declared(asset("jre", "any", JVM, file="jre.tar.gz", unpack=True))
        bundle = tmp_path / "bundle"
        bundle.mkdir()

        root = AssetLayout(cached(tmp_path, JVM)).lay_out(bundle, [jre])

        java = root / "jre" / "jre" / "bin" / "java"
        assert root == bundle / "assets"
        assert java.read_bytes().startswith(b"#!/bin/sh")
        if os.name == "posix":
            assert java.stat().st_mode & stat.S_IXUSR
            assert not java.stat().st_mode & stat.S_IWUSR
            with pytest.raises(PermissionError):
                (root / "jre" / "jre" / "new").write_text("x")

    def test_a_file_is_laid_out_as_it_is(self, tmp_path):
        [model] = declared(asset("model", "any", MODEL, file="model.onnx"))
        bundle = tmp_path / "bundle"
        bundle.mkdir()

        root = AssetLayout(cached(tmp_path, MODEL)).lay_out(bundle, [model])

        assert (root / "model" / "model.onnx").read_bytes() == MODEL

    def test_one_laid_out_already_is_left_alone(self, tmp_path):
        [model] = declared(asset("model", "any", MODEL, file="model.onnx"))
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        layout = AssetLayout(cached(tmp_path, MODEL))
        layout.lay_out(bundle, [model])
        before = (bundle / "assets" / "model" / "model.onnx").stat().st_mtime_ns

        layout.lay_out(bundle, [model])

        assert (bundle / "assets" / "model" / "model.onnx").stat().st_mtime_ns == before

    def test_bytes_other_than_the_pinned_ones_are_not_laid_out(self, tmp_path):
        [model] = declared(asset("model", "any", MODEL, file="model.onnx"))
        cache = cached(tmp_path, MODEL)
        cache.path(model).chmod(0o644)
        cache.path(model).write_bytes(b"the same length, other bytes"[: len(MODEL)])
        bundle = tmp_path / "bundle"
        bundle.mkdir()

        with pytest.raises(AssetError, match="not on this machine"):
            AssetLayout(cache).lay_out(bundle, [model])

        assert not (bundle / "assets" / "model").exists()

    def test_a_tar_reaching_outside_itself_is_refused(self, tmp_path):
        evil = tar_gz({"../../escaped": b"x"})
        [bad] = declared(asset("bad", "any", evil, file="bad.tar.gz", unpack=True))
        bundle = tmp_path / "bundle"
        bundle.mkdir()

        with pytest.raises(AssetError, match="could not be unpacked"):
            AssetLayout(cached(tmp_path, evil)).lay_out(bundle, [bad])

        assert not (tmp_path / "escaped").exists()
        assert not (bundle / "assets" / "bad").exists()

    def test_a_zip_reaching_outside_itself_is_refused(self, tmp_path):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zipped:
            zipped.writestr("../escaped", b"x")
        evil = buffer.getvalue()
        [bad] = declared(asset("bad", "any", evil, file="bad.zip", unpack=True))
        bundle = tmp_path / "bundle"
        bundle.mkdir()

        with pytest.raises(AssetError, match="outside itself"):
            AssetLayout(cached(tmp_path, evil)).lay_out(bundle, [bad])

    def test_a_zip_unpacks(self, tmp_path):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zipped:
            zipped.writestr("jre/bin/java.exe", b"MZ")
        body = buffer.getvalue()
        [jre] = declared(asset("jre", "any", body, file="jre.zip", unpack=True))
        bundle = tmp_path / "bundle"
        bundle.mkdir()

        root = AssetLayout(cached(tmp_path, body)).lay_out(bundle, [jre])

        assert (root / "jre" / "jre" / "bin" / "java.exe").read_bytes() == b"MZ"

    def test_what_was_sealed_can_still_be_removed(self, tmp_path):
        [jre] = declared(asset("jre", "any", JVM, file="jre.tar.gz", unpack=True))
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        AssetLayout(cached(tmp_path, JVM)).lay_out(bundle, [jre])

        ReadOnlyTree.remove(bundle)

        assert not bundle.exists()


class Downloads:
    """Where a development install's assets come from: their ``source``."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.asked: list[str] = []

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.asked.append(str(request.url))
            body = self.files.get(str(request.url))
            return httpx.Response(200, content=body) if body is not None else httpx.Response(404)

        return httpx.MockTransport(handle)


def installer(
    tmp_path: Path, store: ExtensionStore, mcp: StubMcp, **kwargs: Any
) -> ExtensionInstaller:
    registry = ExtensionRegistry()
    return ExtensionInstaller(
        registry=registry,
        rpc=ExtensionRpc(registry),
        mcp=mcp,
        store=store,
        environments=PythonEnvironments(tmp_path / "envs", runner=Pretending()),
        **kwargs,
    )


class TestInstalling:
    async def test_a_development_install_fetches_what_is_missing_from_its_source(self, tmp_path):
        assets = every_asset()
        bundle = bundle_with_wheels(tmp_path, assets=assets)
        downloads = Downloads(
            {
                a["source"]: (JVM if a["platform"] == str(HERE) else MODEL)
                for a in assets
                if a["platform"] in (str(HERE), "any")
            }
        )
        store = ExtensionStore(tmp_path / "store")
        mcp = StubMcp()

        entry = await installer(
            tmp_path,
            store,
            mcp,
            allow_development=True,
            asset_source=AssetSource(transport=downloads.transport()),
        ).install(bundle)

        assert entry.state is ExtensionState.READY, entry.reason
        assert sorted(downloads.asked) == sorted(
            a["source"] for a in assets if a["platform"] in (str(HERE), "any")
        )
        env = mcp.configs["ext:acme.notes"].env
        assert env[ASSETS_DIR_ENV] == str(bundle / "assets")
        assert (bundle / "assets" / "model" / "model.onnx").read_bytes() == MODEL
        assert (bundle / "assets" / "jre" / "jre" / "bin" / "java").is_file()

    async def test_a_distributed_extension_never_reaches_past_igni(self, tmp_path):
        bundle = bundle_with_wheels(tmp_path, assets=every_asset())
        downloads = Downloads({})
        mcp = StubMcp()

        entry = await installer(
            tmp_path,
            ExtensionStore(tmp_path / "store"),
            mcp,
            asset_source=AssetSource(transport=downloads.transport()),
        ).install(
            bundle,
            distribution=DistributionRecord(
                extension_id="acme.notes",
                version="1.0.0",
                sha256="a" * 64,
                service_url="https://notes.example.com",
            ),
        )

        assert entry.state is ExtensionState.FAILED
        assert "is not on this machine" in entry.reason
        assert downloads.asked == []

    async def test_a_source_serving_other_bytes_is_refused(self, tmp_path):
        assets = every_asset()
        bundle = bundle_with_wheels(tmp_path, assets=assets)
        downloads = Downloads({a["source"]: b"x" * a["size"] for a in assets})

        entry = await installer(
            tmp_path,
            ExtensionStore(tmp_path / "store"),
            StubMcp(),
            allow_development=True,
            asset_source=AssetSource(transport=downloads.transport()),
        ).install(bundle)

        assert entry.state is ExtensionState.FAILED
        assert "pins" in entry.reason


class TestDistribution:
    def _publish(self, world: World, assets: list[dict[str, Any]], version: str = "1.0.0") -> None:
        world.publish(manifest(version=version, assets=assets))
        for raw in assets:
            body = (
                JVM
                if raw["platform"] == str(HERE)
                else MODEL
                if raw["name"] == "model"
                else OTHER[AssetPlatform(raw["platform"])]
            )
            world.igni.assets[raw["sha256"]] = body

    async def test_only_this_platform_s_assets_are_downloaded(self, tmp_path):
        world = World(tmp_path)
        assets = every_asset()
        self._publish(world, assets)

        report = await world.distribution.sync()

        assert report.installed == ["acme.notes"], report
        wanted = {a["sha256"] for a in assets if a["platform"] in (str(HERE), "any")}
        assert set(world.igni.asset_downloads()) == wanted
        assert {p.name for p in world.store.asset_cache.root.iterdir()} == wanted

    async def test_an_asset_on_this_machine_is_not_downloaded_again(self, tmp_path):
        world = World(tmp_path)
        assets = every_asset()
        self._publish(world, assets)
        await world.distribution.sync()
        before = len(world.igni.asset_downloads())

        self._publish(world, assets, version="1.0.1")
        report = await world.distribution.sync()

        assert report.installed == ["acme.notes"], report
        assert len(world.igni.asset_downloads()) == before

    async def test_other_bytes_from_igni_leave_the_installed_version(self, tmp_path):
        world = World(tmp_path)
        assets = every_asset()
        self._publish(world, assets)
        await world.distribution.sync()
        newer = [
            *[a for a in assets if a["name"] != "model"],
            asset("model", "any", b"a newer model", file="model.onnx"),
        ]
        world.publish(manifest(version="1.1.0", assets=newer))
        world.igni.assets[newer[-1]["sha256"]] = b"a NEWER model"

        report = await world.distribution.sync()

        assert "acme.notes" in report.failed
        assert (
            json.loads((world.store.path_for("acme.notes") / "manifest.json").read_text())[
                "version"
            ]
            == "1.0.0"
        )

    async def test_assets_no_installed_extension_declares_are_forgotten(self, tmp_path):
        world = World(tmp_path)
        self._publish(world, every_asset())
        await world.distribution.sync()

        world.igni.withdraw("acme.notes")
        await world.distribution.sync()

        assert list(world.store.asset_cache.root.iterdir()) == []

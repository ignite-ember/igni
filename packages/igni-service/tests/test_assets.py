"""An extension's assets: declared and pinned, fetched at build time, uploaded signed.

The fetches go to an `httpx.MockTransport`, so nothing here touches a
network; the wheelhouse is built offline as in `test_bundle.py`.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import tarfile
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from igni_service import BundleError, IgniService, MemoryKeyStore
from igni_service.assets import (
    AssetFetcher,
    AssetPlatform,
    BundleAsset,
    BundleAssets,
    BundleLayout,
)
from igni_service.bundle import PLATFORMS, BundleArchive, BundleBuilder
from igni_service.registration import ASSET_SIGNATURE_HEADER, ExtensionBundle
from igni_service.testing import FakeIgni
from igni_service.urls import IgniPaths

from .test_bundle import index as index  # noqa: F401 — the fixture
from .test_bundle import write_extension

JRE = {platform: f"jre for {platform}".encode() for platform in AssetPlatform.specific()}
MODEL = b"a model, the same everywhere"


def declared(
    asset: str, platform: AssetPlatform, body: bytes, **overrides: object
) -> dict[str, object]:
    name = asset
    return {
        "name": name,
        "platform": str(platform),
        "file": f"{name}-{platform}.tar.gz",
        "size": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "source": f"https://downloads.test/{name}/{platform}.tar.gz",
        "unpack": True,
        **overrides,
    }


def every_asset() -> list[dict[str, object]]:
    return [
        *(declared("jre", platform, body) for platform, body in JRE.items()),
        declared("model", AssetPlatform.ANY, MODEL, file="model.onnx", unpack=False),
    ]


class Downloads:
    """What `downloads.test` serves, and what was asked of it."""

    def __init__(self) -> None:
        self.files = {f"/jre/{p}.tar.gz": body for p, body in JRE.items()}
        self.files["/model/any.tar.gz"] = MODEL
        self.asked: list[str] = []

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.asked.append(request.url.path)
            body = self.files.get(request.url.path)
            return httpx.Response(200, content=body) if body is not None else httpx.Response(404)

        return httpx.MockTransport(handle)


def with_assets(extension: Path, assets: list[dict[str, object]]) -> Path:
    manifest = json.loads((extension / "manifest.json").read_text())
    manifest["assets"] = assets
    (extension / "manifest.json").write_text(json.dumps(manifest))
    return extension


class TestTheDeclaration:
    def test_a_name_is_one_file_for_every_platform(self) -> None:
        BundleAssets.model_validate({"assets": [declared("model", AssetPlatform.ANY, MODEL)]})

    def test_or_one_for_each_platform_igni_runs_on(self) -> None:
        BundleAssets.model_validate({"assets": every_asset()})

    def test_a_platform_left_out_is_refused(self) -> None:
        two = [declared("jre", p, JRE[p]) for p in AssetPlatform.specific()[:2]]
        with pytest.raises(ValidationError, match="one for each of"):
            BundleAssets.model_validate({"assets": two})

    def test_any_beside_a_platform_is_refused(self) -> None:
        mixed = [
            declared("jre", AssetPlatform.ANY, MODEL),
            declared("jre", AssetPlatform.MACOS_ARM64, JRE[AssetPlatform.MACOS_ARM64]),
        ]
        with pytest.raises(ValidationError):
            BundleAssets.model_validate({"assets": mixed})

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("file", "../escape.tar.gz"),
            ("file", "dir/file.tar.gz"),
            ("source", "http://downloads.test/plain.tar.gz"),
            ("sha256", "ABC"),
            ("size", 0),
            ("name", "Has Spaces"),
        ],
    )
    def test_what_could_escape_or_is_unpinned_is_refused(self, field: str, value: object) -> None:
        with pytest.raises(ValidationError):
            BundleAsset.model_validate(
                declared("model", AssetPlatform.ANY, MODEL, **{field: value})
            )

    def test_only_an_archive_can_be_unpacked(self) -> None:
        with pytest.raises(ValidationError, match="can be unpacked"):
            BundleAsset.model_validate(
                declared("model", AssetPlatform.ANY, MODEL, file="model.onnx", unpack=True)
            )

    def test_a_machine_gets_its_own_platform_and_the_shared_ones(self) -> None:
        assets = BundleAssets.model_validate({"assets": every_asset()})
        mine = assets.for_platform(AssetPlatform.LINUX_X86_64)
        assert sorted((a.name, a.platform) for a in mine) == [
            ("jre", AssetPlatform.LINUX_X86_64),
            ("model", AssetPlatform.ANY),
        ]

    def test_the_assets_sit_beside_the_bundle(self) -> None:
        assert BundleLayout.assets_dir(Path("/b/extension-bundle.tar.gz")) == Path(
            "/b/extension-bundle.assets"
        )


class TestFetching:
    def test_each_is_written_by_its_digest(self, tmp_path: Path) -> None:
        downloads = Downloads()
        asset = BundleAsset.model_validate(
            declared("model", AssetPlatform.ANY, MODEL, file="model.onnx", unpack=False)
        )
        path = AssetFetcher(transport=downloads.transport()).fetch(
            asset.model_copy(update={"source": "https://downloads.test/model/any.tar.gz"}),
            tmp_path,
        )
        assert path == tmp_path / asset.sha256
        assert path.read_bytes() == MODEL

    def test_one_already_there_is_not_fetched_again(self, tmp_path: Path) -> None:
        downloads = Downloads()
        asset = BundleAsset.model_validate(
            declared(
                "model", AssetPlatform.ANY, MODEL, source="https://downloads.test/model/any.tar.gz"
            )
        )
        fetcher = AssetFetcher(transport=downloads.transport())
        fetcher.fetch(asset, tmp_path)
        fetcher.fetch(asset, tmp_path)
        assert downloads.asked == ["/model/any.tar.gz"]

    def test_bytes_other_than_the_pinned_ones_stop_the_build(self, tmp_path: Path) -> None:
        downloads = Downloads()
        downloads.files["/model/any.tar.gz"] = b"something else, the same length!!"[: len(MODEL)]
        asset = BundleAsset.model_validate(
            declared(
                "model", AssetPlatform.ANY, MODEL, source="https://downloads.test/model/any.tar.gz"
            )
        )
        with pytest.raises(BundleError, match="pins"):
            AssetFetcher(transport=downloads.transport()).fetch(asset, tmp_path)
        assert list(tmp_path.iterdir()) == []

    def test_a_larger_file_is_refused_while_it_downloads(self, tmp_path: Path) -> None:
        downloads = Downloads()
        downloads.files["/model/any.tar.gz"] = MODEL * 3
        asset = BundleAsset.model_validate(
            declared(
                "model", AssetPlatform.ANY, MODEL, source="https://downloads.test/model/any.tar.gz"
            )
        )
        with pytest.raises(BundleError, match="larger than"):
            AssetFetcher(transport=downloads.transport()).fetch(asset, tmp_path)

    def test_a_missing_file_says_where_it_looked(self, tmp_path: Path) -> None:
        asset = BundleAsset.model_validate(
            declared("model", AssetPlatform.ANY, MODEL, source="https://downloads.test/gone.tar.gz")
        )
        with pytest.raises(BundleError, match="answered 404"):
            AssetFetcher(transport=Downloads().transport()).fetch(asset, tmp_path)


class TestTheBuilder:
    def test_assets_are_written_beside_the_archive_and_not_into_it(
        self, tmp_path: Path, index: Path
    ) -> None:
        extension = with_assets(
            write_extension(tmp_path / "extension", dependencies=[]),
            [
                *(declared("jre", p, body) for p, body in JRE.items()),
                declared(
                    "model",
                    AssetPlatform.ANY,
                    MODEL,
                    source="https://downloads.test/model/any.tar.gz",
                ),
            ],
        )
        out = tmp_path / "extension-bundle.tar.gz"
        (BundleLayout.assets_dir(out)).mkdir()
        (BundleLayout.assets_dir(out) / ("0" * 64)).write_bytes(b"left from an older build")

        report = BundleBuilder(
            extension,
            find_links=[index],
            no_index=True,
            build_isolation=False,
            fetcher=AssetFetcher(transport=Downloads().transport()),
        ).build(out)

        assert report.assets_dir == tmp_path / "extension-bundle.assets"
        assert sorted(p.name for p in report.assets_dir.iterdir()) == sorted(
            hashlib.sha256(body).hexdigest() for body in [*JRE.values(), MODEL]
        )
        with tarfile.open(fileobj=io.BytesIO(out.read_bytes()), mode="r:gz") as tar:
            assert not [m.name for m in tar.getmembers() if m.name.startswith("assets")]
        shipped = ExtensionBundle.at(out)
        assert shipped is not None
        assert {a.name for a in shipped.assets} == {"jre", "model"}

    def test_an_unpinned_asset_stops_the_build_before_anything_is_fetched(
        self, tmp_path: Path, index: Path
    ) -> None:
        downloads = Downloads()
        extension = with_assets(
            write_extension(tmp_path / "extension", dependencies=[]),
            [declared("jre", AssetPlatform.MACOS_ARM64, JRE[AssetPlatform.MACOS_ARM64])],
        )
        with pytest.raises(BundleError, match="pin every asset"):
            BundleBuilder(
                extension,
                find_links=[index],
                no_index=True,
                build_isolation=False,
                fetcher=AssetFetcher(transport=downloads.transport()),
            ).build(tmp_path / "b.tar.gz")
        assert downloads.asked == []


class TestMacOS:
    def test_the_bundle_is_for_macos_26_and_takes_older_wheels(self) -> None:
        """uv resolves for 26; pip widens the 26 tag to every older macOS itself."""
        [mac] = [p for p in PLATFORMS if p.asset is AssetPlatform.MACOS_ARM64]
        assert mac.pip == ("macosx_26_0_arm64",)
        assert dict(mac.uv_env) == {"MACOSX_DEPLOYMENT_TARGET": "26.0"}


def bundle_with_assets(tmp_path: Path) -> Path:
    staging = tmp_path / "staging"
    staging.mkdir()
    manifest = {
        "id": "acme.example",
        "version": "1.0.0",
        "python": {"package": "acme-example", "entry": "acme-example", "wheels": "wheels"},
        "assets": every_asset(),
    }
    (staging / "manifest.json").write_text(json.dumps(manifest))
    out = tmp_path / "extension-bundle.tar.gz"
    BundleArchive.write(staging, out)
    assets = BundleLayout.assets_dir(out)
    assets.mkdir()
    for body in [*JRE.values(), MODEL]:
        (assets / hashlib.sha256(body).hexdigest()).write_bytes(body)
    return out


async def run_lifespan(igni: IgniService) -> None:
    app = FastAPI(lifespan=igni.lifespan)
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(igni.registration.registered.wait(), timeout=5)
        await asyncio.sleep(0.05)


class TestShipping:
    async def test_assets_go_up_signed_before_the_bundle(
        self, fake: FakeIgni, tmp_path: Path
    ) -> None:
        bundle = bundle_with_assets(tmp_path)
        igni = fake.service(fake.settings(extension_bundle_path=str(bundle)))

        await run_lifespan(igni)

        client_id = await igni.registration.client_id()
        puts = [s for s in fake.seen if s.method == "PUT"]
        assert [s.path.rsplit("/", 1)[-1] for s in puts][-1] == "extension"
        assets = [s for s in puts if "/extension/assets/" in s.path]
        assert len(assets) == 4
        assert all(s.headers[ASSET_SIGNATURE_HEADER.lower()] for s in assets)
        assert {sha for (owner, sha) in fake.assets if owner == client_id} == {
            hashlib.sha256(body).hexdigest() for body in [*JRE.values(), MODEL]
        }
        assert fake.extensions[client_id].version == "1.0.0"

    async def test_an_asset_igni_holds_is_not_sent_again(
        self, fake: FakeIgni, tmp_path: Path
    ) -> None:
        bundle = bundle_with_assets(tmp_path)
        store = MemoryKeyStore()
        await run_lifespan(
            fake.service(fake.settings(extension_bundle_path=str(bundle)), key_store=store)
        )
        client_id = next(iter(fake.clients))
        del fake.extensions[client_id]

        await run_lifespan(
            fake.service(fake.settings(extension_bundle_path=str(bundle)), key_store=store)
        )

        asset_puts = [s for s in fake.seen if s.method == "PUT" and "/assets/" in s.path]
        assert len(asset_puts) == 4
        assert fake.extensions[client_id].version == "1.0.0"
        assert IgniPaths.extension(client_id)

    async def test_a_damaged_asset_is_not_uploaded(self, fake: FakeIgni, tmp_path: Path) -> None:
        bundle = bundle_with_assets(tmp_path)
        (BundleLayout.assets_dir(bundle) / hashlib.sha256(MODEL).hexdigest()).write_bytes(b"x")
        shipped = ExtensionBundle.at(bundle)
        assert shipped is not None
        igni = fake.service(fake.settings())
        await igni.registration.register()

        with pytest.raises(BundleError, match="build the bundle again"):
            await igni.registration.ship(shipped)

        assert fake.extensions == {}

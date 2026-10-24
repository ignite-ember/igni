"""Registering on start, logging the thumbprint, and shipping the extension when it changed."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest
from fastapi import FastAPI

from igni_service import IgniService, MemoryKeyStore
from igni_service.bundle import BundleArchive
from igni_service.registration import SIGNATURE_HEADER
from igni_service.testing import FakeIgni
from igni_service.urls import IgniPaths


def write_bundle(
    tmp_path: Path, *, version: str = "1.0.0", extension_id: str = "acme.example"
) -> Path:
    staging = tmp_path / f"staging-{version}"
    staging.mkdir(exist_ok=True)
    manifest = {
        "id": extension_id,
        "version": version,
        "contributes": {"pages": [{"kind": "acme.example", "label": "Example"}]},
        "python": {"package": "acme-example", "entry": "acme-example", "wheels": "wheels"},
    }
    (staging / "manifest.json").write_text(json.dumps(manifest))
    out = tmp_path / "extension-bundle.tar.gz"
    BundleArchive.write(staging, out)
    return out


async def run_lifespan(igni: IgniService) -> None:
    """Start the app as uvicorn would, wait for the registration, stop."""
    app = FastAPI(lifespan=igni.lifespan)
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(igni.registration.registered.wait(), timeout=5)
        await asyncio.sleep(0.05)


async def test_starting_registers_the_service_with_its_own_public_key(fake: FakeIgni) -> None:
    igni = fake.service(fake.settings(public_base_url="https://Service.test/", service_name="Acme"))

    await run_lifespan(igni)

    [seen] = fake.requests_to(IgniPaths.CLIENTS)
    body = seen.json_body()
    assert body["client_name"] == "Acme"
    assert body["client_uri"] == "https://service.test"
    assert body["kind"] == "service"
    assert body["token_endpoint_auth_method"] == "private_key_jwt"
    [key] = body["jwks"]["keys"]
    assert key["kty"] == "RSA" and key["alg"] == "RS256"
    assert "d" not in key
    assert key["kid"] == await igni.thumbprint()
    assert igni.registration.current is not None
    assert igni.registration.current.state == "pending"


async def test_the_thumbprint_is_logged_for_the_super_admin_to_compare(
    fake: FakeIgni, igni: IgniService, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="igni_service"):
        await run_lifespan(igni)

    assert await igni.thumbprint() in caplog.text
    assert "approves this service" in caplog.text


async def test_a_restart_with_the_same_key_finds_the_same_registration(fake: FakeIgni) -> None:
    store = MemoryKeyStore()
    first = fake.service(key_store=store)
    second = fake.service(key_store=store)

    await run_lifespan(first)
    await run_lifespan(second)

    assert len(fake.clients) == 1
    assert await first.registration.client_id() == await second.registration.client_id()


async def test_registration_retries_until_igni_answers(fake: FakeIgni) -> None:
    fake.failing[IgniPaths.CLIENTS] = 503
    igni = fake.service()
    app = FastAPI(lifespan=igni.lifespan)

    async with app.router.lifespan_context(app):
        while len(fake.requests_to(IgniPaths.CLIENTS)) < 2:
            await asyncio.sleep(0.01)
        assert not igni.registration.registered.is_set()
        fake.failing.clear()
        await asyncio.wait_for(igni.registration.registered.wait(), timeout=5)

    assert len(fake.clients) == 1


async def test_the_bundle_is_uploaded_signed_and_only_when_it_changed(
    fake: FakeIgni, tmp_path: Path
) -> None:
    bundle = write_bundle(tmp_path)
    store = MemoryKeyStore()
    igni = fake.service(fake.settings(extension_bundle_path=str(bundle)), key_store=store)

    await run_lifespan(igni)

    client_id = await igni.registration.client_id()
    [upload] = [s for s in fake.requests_to(IgniPaths.extension(client_id)) if s.method == "PUT"]
    assert upload.headers["content-type"] == "application/gzip"
    assert upload.headers[SIGNATURE_HEADER.lower()]
    assert fake.extensions[client_id].extension_id == "acme.example"
    assert fake.extensions[client_id].version == "1.0.0"

    again = fake.service(fake.settings(extension_bundle_path=str(bundle)), key_store=store)
    await run_lifespan(again)
    puts = [s for s in fake.requests_to(IgniPaths.extension(client_id)) if s.method == "PUT"]
    assert len(puts) == 1

    bundle.unlink()
    changed = write_bundle(tmp_path, version="1.0.1")
    third = fake.service(fake.settings(extension_bundle_path=str(changed)), key_store=store)
    await run_lifespan(third)
    puts = [s for s in fake.requests_to(IgniPaths.extension(client_id)) if s.method == "PUT"]
    assert len(puts) == 2
    assert fake.extensions[client_id].version == "1.0.1"


async def test_a_refused_bundle_is_logged_and_not_retried(
    fake: FakeIgni, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    bundle = write_bundle(tmp_path)
    shipper = fake.service(fake.settings(extension_bundle_path=str(bundle)))
    await shipper.registration.keep_trying()
    fake.approve(await shipper.registration.client_id())
    bundle.unlink()
    rival = fake.service(fake.settings(extension_bundle_path=str(write_bundle(tmp_path))))

    with caplog.at_level(logging.ERROR, logger="igni_service"):
        assert await asyncio.wait_for(rival.registration.keep_trying(), timeout=5) is not None

    assert "refused extension acme.example" in caplog.text
    assert "409" in caplog.text
    assert len(fake.requests_to(IgniPaths.CLIENTS)) == 2


async def test_an_unreadable_bundle_leaves_the_registration_standing(
    fake: FakeIgni, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    broken = tmp_path / "broken.tar.gz"
    broken.write_bytes(b"not a tarball")
    igni = fake.service(fake.settings(extension_bundle_path=str(broken)))

    with caplog.at_level(logging.ERROR, logger="igni_service"):
        registration = await asyncio.wait_for(igni.registration.keep_trying(), timeout=5)

    assert registration is not None
    assert "unreadable" in caplog.text
    assert fake.extensions == {}

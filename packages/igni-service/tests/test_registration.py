"""Registering on start, logging the thumbprint, and shipping the extension when it changed."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

import igni_service
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


async def test_callers_during_start_up_share_the_one_registration(fake: FakeIgni) -> None:
    """A page asking for the client id while start-up registers does not register again.

    The check that a registration was held ran outside the lock, so every
    caller that arrived while the first one was in flight registered once
    more when the lock came free: two POST /v1/clients per Team Notes start.
    """
    inner = fake.transport()

    class InFlight(httpx.AsyncBaseTransport):
        """igni over a network: a request is in flight for a moment, and others run meanwhile."""

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(0.01)
            return await inner.handle_async_request(request)

    igni = IgniService(
        fake.settings(),
        key_store=MemoryKeyStore(),
        http=httpx.AsyncClient(transport=InFlight()),
        clock=fake.clock,
        retry_min=0.01,
    )

    ids = await asyncio.gather(
        igni.registration.start(),
        igni.registration.client_id(),
        igni.registration.client_id(),
    )

    assert len(fake.requests_to(IgniPaths.CLIENTS)) == 1
    assert ids[1] == ids[2] == ids[0].client_id


class TestTheStartUpSaysWhatComesNext:
    """The banner's last lines are the next step for the state igni answered."""

    async def test_an_approved_service_is_not_told_to_get_approved(
        self, fake: FakeIgni, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = MemoryKeyStore()
        await run_lifespan(fake.service(key_store=store))
        fake.approve()

        caplog.clear()
        with caplog.at_level(logging.INFO, logger="igni_service"):
            await run_lifespan(fake.service(key_store=store))

        assert "approves this service" not in caplog.text
        assert "approved" in caplog.text
        assert "org admin" in caplog.text  # who switches the extension on for a group

    async def test_a_revoked_service_is_told_it_was_revoked(
        self, fake: FakeIgni, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = MemoryKeyStore()
        await run_lifespan(fake.service(key_store=store))
        fake.revoke()

        caplog.clear()
        with caplog.at_level(logging.INFO, logger="igni_service"):
            await run_lifespan(fake.service(key_store=store))

        assert "revoked" in caplog.text
        assert "approves this service" not in caplog.text

    async def test_a_held_update_is_said_with_igni_s_reason(
        self, fake: FakeIgni, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = MemoryKeyStore()
        first = write_bundle(tmp_path)
        await run_lifespan(
            fake.service(fake.settings(extension_bundle_path=str(first)), key_store=store)
        )
        fake.approve()
        fake.hold_uploads = "it asks for more than the approved version"
        first.unlink()
        wider = write_bundle(tmp_path, version="1.1.0")

        with caplog.at_level(logging.INFO, logger="igni_service"):
            await run_lifespan(
                fake.service(fake.settings(extension_bundle_path=str(wider)), key_store=store)
            )

        assert "held" in caplog.text
        assert "asks for more than the approved version" in caplog.text
        assert "1.0.0" in caplog.text or "approved version" in caplog.text


def test_its_lines_show_under_uvicorn_which_configures_only_its_own_loggers(
    tmp_path: Path,
) -> None:
    """uvicorn gives the root logger no handler, so an INFO line from the SDK went nowhere."""
    bundle = write_bundle(tmp_path)
    script = tmp_path / "serve.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import asyncio, logging
            from fastapi import FastAPI
            from igni_service.testing import FakeIgni

            # What uvicorn's LOGGING_CONFIG does: its own loggers, not the root.
            logging.getLogger("uvicorn").addHandler(logging.StreamHandler())

            async def main():
                fake = FakeIgni()
                igni = fake.service(fake.settings(extension_bundle_path={str(bundle)!r}))
                app = FastAPI(lifespan=igni.lifespan)
                async with app.router.lifespan_context(app):
                    await asyncio.wait_for(igni.registration.registered.wait(), 5)
                    await asyncio.sleep(0.2)

            asyncio.run(main())
            """
        )
    )
    ran = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(Path(igni_service.__file__).parent.parent)},
    )

    assert ran.returncode == 0, ran.stderr
    assert "uploaded extension acme.example 1.0.0" in ran.stderr
    assert "INFO" in ran.stderr

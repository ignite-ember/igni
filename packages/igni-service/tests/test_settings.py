"""Starting a service from its environment."""

from __future__ import annotations

from pathlib import Path

import pytest

from igni_service import (
    ConfigurationError,
    IgniService,
    IgniSettings,
    KeyCipher,
    SqlKeyStore,
    SqlWebSessionStore,
)
from igni_service.testing import FakeIgni


async def test_the_environment_names_igni_the_service_and_its_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    key = KeyCipher.generate()
    monkeypatch.setenv("IGNI_SERVER_BASE_URL", "http://igni-api:8000")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://Acme.example.test/")
    monkeypatch.setenv("SERVICE_NAME", "Acme")
    monkeypatch.setenv("IGNI_KEY_ENCRYPTION_KEY", key)
    monkeypatch.setenv("IGNI_KEY_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'k.db'}")
    monkeypatch.setenv("IGNI_EXTENSION_BUNDLE", "/app/extension-bundle.tar.gz")

    igni = IgniService.from_settings(IgniSettings())

    assert igni.server.base_url == "http://igni-api:8000"
    assert igni.server.own_url == "https://acme.example.test"
    assert isinstance(igni.key.store, SqlKeyStore)
    assert isinstance(igni.web.store, SqlWebSessionStore)
    assert igni.settings.extension_bundle_path == "/app/extension-bundle.tar.gz"
    await igni.aclose()


async def test_the_key_from_settings_is_kept_in_the_database(tmp_path: Path) -> None:
    fake = FakeIgni()
    settings = fake.settings(
        key_encryption_key=KeyCipher.generate(),
        key_database_url=f"sqlite+aiosqlite:///{tmp_path / 'k.db'}",
    )

    first = IgniService.from_settings(settings, http=fake.http_client())
    second = IgniService.from_settings(settings, http=fake.http_client())

    assert await first.thumbprint() == await second.thumbprint()
    await first.aclose()
    await second.aclose()


@pytest.mark.parametrize("missing", ["igni_server_url", "public_base_url", "service_name"])
def test_a_service_without_its_addresses_does_not_start(missing: str) -> None:
    fake = FakeIgni()
    settings = fake.settings(**{missing: ""})

    with pytest.raises(ConfigurationError):
        fake.service(settings)


def test_a_public_url_that_is_not_one_is_said_at_start() -> None:
    fake = FakeIgni()

    with pytest.raises(ConfigurationError, match="PUBLIC_BASE_URL"):
        fake.service(fake.settings(public_base_url="artifacts.example.test"))


def test_with_nowhere_to_keep_the_key_the_service_says_so() -> None:
    with pytest.raises(ConfigurationError, match="IGNI_KEY_DATABASE_URL"):
        IgniService.from_settings(FakeIgni().settings())

"""Extensions have no marketplace.

They had one: ``igni ext marketplace add`` registered a catalog,
igni read an ``extensions`` array beside the ``plugins`` one in
``.claude-plugin/marketplace.json``, and ``igni ext install
@acme/scanner`` installed a row from it. All of that is gone. An
extension reaches a machine from its service, through igni server
(``docs/service-identity.md`` §8), and the only other way is a
development install of a directory or a repository.

**Plugin marketplaces are untouched**, and a catalog that still
carries an ``extensions`` array keeps parsing — its plugins are as
installable as ever, and nothing reads the rest.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from ember_code.cli.ext import ext
from ember_code.core.plugins.marketplace_store import MarketplaceRegistryStore
from ember_code.core.plugins.models import (
    InstallRef,
    MarketplaceCatalog,
    MarketplaceEntry,
    MarketplaceRegistry,
)

#: A catalog written for the extension marketplace that existed.
OLD_CATALOG = {
    "name": "acme",
    "plugins": [{"name": "p", "source": "https://x/p"}],
    "extensions": [{"name": "scanner", "id": "acme.scanner", "source": "./extensions/scanner"}],
}


@pytest.fixture
def dev_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IGNI_DEV_EXTENSIONS", "1")


def test_a_catalog_with_an_extensions_array_still_parses_for_its_plugins():
    catalog = MarketplaceCatalog.model_validate(OLD_CATALOG)
    assert [p.name for p in catalog.plugins] == ["p"]
    # Kept as an unread extra, not modelled.
    assert "extensions" not in MarketplaceCatalog.model_fields


def test_a_plugin_ref_still_resolves():
    registry = MarketplaceRegistry(
        marketplaces=[
            MarketplaceEntry(
                name="acme",
                url="https://git/acme",
                cached=MarketplaceCatalog.model_validate(OLD_CATALOG),
            )
        ]
    )
    ref = InstallRef.parse("@acme/p")
    assert ref is not None
    resolved = registry.resolve_install_ref(ref)
    assert resolved is not None and resolved[0].url == "https://x/p"


def test_nothing_resolves_an_extension_ref_any_more():
    assert not hasattr(MarketplaceRegistry, "resolve_extension_ref")
    assert not hasattr(MarketplaceRegistryStore, "resolve_extension_ref")


def test_there_is_no_ext_marketplace_command():
    done = CliRunner().invoke(ext, ["marketplace", "list"])
    assert done.exit_code != 0
    assert "No such command" in done.output


def test_an_at_ref_is_refused_saying_where_extensions_come_from(dev_flag, tmp_path: Path):
    """Not cloned, not looked up: ``@acme/scanner`` names a
    marketplace row, and there are no marketplace rows."""
    done = CliRunner().invoke(ext, ["install", "@acme/scanner", "--root", str(tmp_path / "store")])
    assert done.exit_code != 0
    assert "no marketplace" in done.output
    assert "through igni" in done.output
    assert not (tmp_path / "store").exists()

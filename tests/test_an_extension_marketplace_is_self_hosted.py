"""A catalog you run, in the file people already write.

Extensions installed by URL only, so a repository of them was
installable and not discoverable — you had to be told the subdir.
Plugins have had a catalog all along, and the answer is not a
second one.

**The same registry, the same file.** A marketplace is
`.claude-plugin/marketplace.json` in a git repository you control,
and igni reads an `extensions` array beside the `plugins` one. A
Claude Code marketplace still parses, because the model already
allowed extra keys; an igni one carries both lists; and a catalog
added through `igni ext marketplace add` is the same row in the
same `marketplaces.json` that `igni plugin marketplace` writes.

Self-hosted, and only that. There is no index igni publishes and
nothing to submit to — which is the same answer Claude Code gives
for plugins, and the right one for a thing that installs a process
with permissions inside somebody's network.

What is *not* shared is the install command. `igni plugin install
@m/x` will not install an extension and will not fall back to the
extension list: a plugin is a directory of prompts and an
extension is a process, and a ref that quietly resolved to the
wrong kind would be the worst possible convenience.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from ember_code.cli.ext import ext
from ember_code.core.plugins.models import (
    InstallRef,
    MarketplaceCatalog,
    MarketplaceEntry,
    MarketplaceRegistry,
)

FIXTURE = Path(__file__).parent / "fixtures" / "dev_extension"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@x", "-c", "user.name=T", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def catalog_repo(tmp_path: Path) -> Path:
    """A marketplace holding two extensions and no plugins."""
    root = tmp_path / "acme-catalog"
    for name, ident in (("scanner", "acme.scanner"), ("auditor", "acme.auditor")):
        where = root / "extensions" / name
        shutil.copytree(FIXTURE, where)
        manifest = json.loads((where / "manifest.json").read_text())
        manifest["id"] = ident
        (where / "manifest.json").write_text(json.dumps(manifest))
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps(
            {
                "name": "acme",
                "description": "Acme's internal tools",
                "extensions": [
                    {
                        "name": "scanner",
                        "id": "acme.scanner",
                        "source": "./extensions/scanner",
                        "description": "Policy violations in this repository",
                    },
                    {
                        "name": "auditor",
                        "id": "acme.auditor",
                        "source": "./extensions/auditor",
                    },
                ],
            }
        )
    )
    _git(root, "init", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "catalog")
    return root


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A registry of our own, so the suite never writes to ~."""
    where = tmp_path / "home"
    where.mkdir()
    monkeypatch.setenv("HOME", str(where))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: where))
    return where


class TestTheCatalogFormat:
    def test_extensions_sit_beside_plugins(self):
        catalog = MarketplaceCatalog.model_validate(
            {
                "name": "acme",
                "plugins": [{"name": "p", "source": "https://x/p"}],
                "extensions": [{"name": "e", "id": "acme.e", "source": "https://x/e"}],
            }
        )
        assert [p.name for p in catalog.plugins] == ["p"]
        assert [e.name for e in catalog.extensions] == ["e"]
        assert catalog.extensions[0].id == "acme.e"

    def test_a_claude_code_marketplace_still_parses(self):
        """The addition is additive. A catalog written before igni
        existed has no `extensions` key and must not become
        unreadable because of one."""
        catalog = MarketplaceCatalog.model_validate(
            {"name": "theirs", "plugins": [{"name": "p", "source": "https://x/p"}]}
        )
        assert catalog.extensions == []

    def test_an_extension_row_resolves_like_a_plugin_row(self):
        """One `resolved_source`, shared. A second copy would be a
        second place for a novel catalog shape to go unhandled."""
        catalog = MarketplaceCatalog.model_validate(
            {
                "name": "acme",
                "extensions": [{"name": "e", "source": "./extensions/e"}],
            }
        )
        resolved = catalog.extensions[0].resolved_source("https://git/acme")
        assert resolved is not None
        assert resolved.kind == "relative"
        assert resolved.url == "https://git/acme"
        assert resolved.subdir == "extensions/e"


class TestTheRefResolvesToTheRightKind:
    def _registry(self) -> MarketplaceRegistry:
        catalog = MarketplaceCatalog.model_validate(
            {
                "name": "acme",
                "plugins": [{"name": "shared", "source": "https://x/plugin"}],
                "extensions": [{"name": "shared", "source": "https://x/extension"}],
            }
        )
        return MarketplaceRegistry(
            marketplaces=[MarketplaceEntry(name="acme", url="https://git/acme", cached=catalog)]
        )

    def test_a_name_in_both_lists_resolves_to_its_own_kind(self):
        """The case that makes a shared lookup wrong. One command
        must never quietly install the other's thing — a plugin is
        a directory of prompts and an extension is a process."""
        ref = InstallRef.parse("@acme/shared")
        assert ref is not None
        registry = self._registry()

        as_plugin = registry.resolve_install_ref(ref)
        as_extension = registry.resolve_extension_ref(ref)
        assert as_plugin is not None and as_extension is not None
        assert as_plugin[0].url == "https://x/plugin"
        assert as_extension[0].url == "https://x/extension"

    def test_a_plugin_only_catalog_offers_no_extension(self):
        registry = MarketplaceRegistry(
            marketplaces=[
                MarketplaceEntry(
                    name="acme",
                    url="https://git/acme",
                    cached=MarketplaceCatalog.model_validate(
                        {"name": "acme", "plugins": [{"name": "p", "source": "https://x/p"}]}
                    ),
                )
            ]
        )
        ref = InstallRef.parse("@acme/p")
        assert ref is not None
        assert registry.resolve_extension_ref(ref) is None


class TestThroughTheCli:
    def test_adding_a_catalog_says_what_is_in_it(self, catalog_repo, home):
        done = CliRunner().invoke(ext, ["marketplace", "add", f"file://{catalog_repo}"])
        assert done.exit_code == 0, done.output
        assert "2 extension(s)" in done.output

    def test_listing_shows_installable_refs(self, catalog_repo, home):
        runner = CliRunner()
        runner.invoke(ext, ["marketplace", "add", f"file://{catalog_repo}"])
        done = runner.invoke(ext, ["marketplace", "list"])
        assert "@acme/scanner" in done.output
        assert "Policy violations" in done.output

    def test_installing_by_name(self, catalog_repo, home, tmp_path):
        """The whole point: a ref somebody can be told, instead of
        a URL and a subdir they have to be walked through."""
        runner = CliRunner()
        runner.invoke(ext, ["marketplace", "add", f"file://{catalog_repo}"])
        done = runner.invoke(ext, ["install", "@acme/scanner", "--root", str(tmp_path / "store")])
        assert done.exit_code == 0, done.output
        assert "acme.scanner" in done.output
        assert (tmp_path / "store" / "acme.scanner").is_dir()

    def test_an_unknown_ref_names_the_fix(self, catalog_repo, home, tmp_path):
        runner = CliRunner()
        runner.invoke(ext, ["marketplace", "add", f"file://{catalog_repo}"])
        done = runner.invoke(ext, ["install", "@acme/nope", "--root", str(tmp_path / "store")])
        assert done.exit_code != 0
        assert "marketplace list" in done.output

    def test_a_ref_with_no_marketplace_registered_is_not_a_git_url(self, home, tmp_path):
        """`@acme/scanner` is never a clonable URL, so falling
        through to the clone path would fail somewhere far away."""
        done = CliRunner().invoke(
            ext, ["install", "@acme/scanner", "--root", str(tmp_path / "store")]
        )
        assert done.exit_code != 0
        assert "does not name an extension" in done.output

    def test_nothing_registered_says_how_to_start(self, home):
        done = CliRunner().invoke(ext, ["marketplace", "list"])
        assert "igni ext marketplace add" in done.output

    def test_removing_forgets_the_catalog(self, catalog_repo, home):
        runner = CliRunner()
        runner.invoke(ext, ["marketplace", "add", f"file://{catalog_repo}"])
        assert runner.invoke(ext, ["marketplace", "remove", "acme"]).exit_code == 0
        assert "no marketplaces registered" in runner.invoke(ext, ["marketplace", "list"]).output

    def test_removing_one_that_is_not_there_says_so(self, home):
        done = CliRunner().invoke(ext, ["marketplace", "remove", "nope"])
        assert done.exit_code != 0


def test_the_registry_is_shared_with_plugins(catalog_repo, home):
    """One room, two doors. A catalog added here is visible to the
    plugin side, because an author offering both should not have to
    be registered twice."""
    from ember_code.core.plugins.marketplace_store import MarketplaceRegistryStore

    CliRunner().invoke(ext, ["marketplace", "add", f"file://{catalog_repo}"])
    registry = MarketplaceRegistryStore().load()
    assert [m.name for m in registry.marketplaces] == ["acme"]

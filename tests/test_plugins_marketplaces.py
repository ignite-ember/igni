"""Tests for the plugin marketplace registry.

Exercises the full marketplace lifecycle: add (probe + fetch +
cache), list, remove, refresh, and ``@<marketplace>/<plugin>``
install resolution. Real ``git`` against local bare repos
(``file://`` URLs) so the tests catch git-side surprises the same
way installer tests do.

Skipped automatically when ``git`` isn't on PATH.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ember_code.core.plugins.git import GitClient
from ember_code.core.plugins.installer import PluginInstaller
from ember_code.core.plugins.marketplaces import (
    MarketplaceCatalog,
    MarketplacePluginEntry,
    MarketplaceRegistry,
    add_marketplace,
    fetch_catalog,
    load_registry,
    refresh_marketplace,
    registry_path,
    remove_marketplace,
    resolve_install_ref,
    save_registry,
)

_GIT_OK = GitClient().is_available()
pytestmark = pytest.mark.skipif(not _GIT_OK, reason="git not available on PATH")


# ── Helpers ────────────────────────────────────────────────────────


def _run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)


def _make_marketplace_repo(
    workdir: Path,
    name: str,
    plugins: list[dict],
    *,
    use_dot_dir: bool = True,
    description: str | None = None,
) -> Path:
    """Plant a marketplace repo carrying ``marketplace.json``.

    ``use_dot_dir`` controls whether the file lands at
    ``.claude-plugin/marketplace.json`` (canonical) or at the repo
    root (fallback). Both should be resolvable by ``fetch_catalog``.
    """
    source = workdir / f"{name}-mkt-source"
    source.mkdir(parents=True)
    _run(["git", "init", "-q", "-b", "main"], cwd=source)
    _run(["git", "config", "user.email", "test@example.com"], cwd=source)
    _run(["git", "config", "user.name", "test"], cwd=source)

    catalog = {"name": name, "plugins": plugins}
    if description is not None:
        catalog["description"] = description

    if use_dot_dir:
        (source / ".claude-plugin").mkdir()
        target = source / ".claude-plugin" / "marketplace.json"
    else:
        target = source / "marketplace.json"
    target.write_text(json.dumps(catalog), encoding="utf-8")

    _run(["git", "add", "-A"], cwd=source)
    _run(["git", "commit", "-q", "-m", "init"], cwd=source)
    return source


def _make_plugin_repo(workdir: Path, name: str, *, version: str = "1.0.0") -> Path:
    """Same as the installer-test helper — plant a real plugin repo."""
    source = workdir / f"{name}-plugin-source"
    source.mkdir(parents=True)
    _run(["git", "init", "-q", "-b", "main"], cwd=source)
    _run(["git", "config", "user.email", "test@example.com"], cwd=source)
    _run(["git", "config", "user.name", "test"], cwd=source)
    (source / ".claude-plugin").mkdir()
    (source / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "version": version}),
        encoding="utf-8",
    )
    _run(["git", "add", "-A"], cwd=source)
    _run(["git", "commit", "-q", "-m", "init"], cwd=source)
    return source


def _file_url(path: Path) -> str:
    return f"file://{path.resolve()}"


# ── Registry persistence ───────────────────────────────────────────


def test_registry_roundtrip(tmp_path: Path) -> None:
    """Save then load yields equivalent registry. The on-disk format
    is the contract — keep it stable so manual edits or alternative
    tools (e.g. a future config UI) interop."""
    catalog = MarketplaceCatalog(
        name="m1",
        plugins=[
            MarketplacePluginEntry(name="p1", source="https://x/y", version="1.0"),
        ],
    )
    from ember_code.core.plugins.marketplaces import MarketplaceEntry

    registry = MarketplaceRegistry(
        marketplaces=[
            MarketplaceEntry(name="m1", url="https://x/y-mkt", cached=catalog),
        ]
    )
    save_registry(registry, data_dir=tmp_path)

    loaded = load_registry(data_dir=tmp_path)
    assert loaded.find("m1") is not None
    assert loaded.find("m1").cached.plugins[0].source == "https://x/y"
    assert registry_path(data_dir=tmp_path) == tmp_path / "marketplaces.json"


def test_registry_load_missing_returns_empty(tmp_path: Path) -> None:
    assert load_registry(data_dir=tmp_path / "nope").marketplaces == []


def test_registry_load_corrupt_returns_empty(tmp_path: Path) -> None:
    """Corrupt registry file logs a warning and returns empty —
    session continues, user can re-add marketplaces manually."""
    path = registry_path(data_dir=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not-json-at-all")
    assert load_registry(data_dir=tmp_path).marketplaces == []


# ── fetch_catalog ──────────────────────────────────────────────────


def test_fetch_catalog_canonical_path(tmp_path: Path) -> None:
    """The canonical location is ``.claude-plugin/marketplace.json``."""
    sources = tmp_path / "sources"
    sources.mkdir()
    mkt = _make_marketplace_repo(
        sources,
        "m1",
        plugins=[{"name": "alpha", "source": "https://x/alpha"}],
    )

    catalog = fetch_catalog(_file_url(mkt))
    assert catalog.name == "m1"
    assert len(catalog.plugins) == 1
    assert catalog.plugins[0].source == "https://x/alpha"


def test_fetch_catalog_root_fallback(tmp_path: Path) -> None:
    """``marketplace.json`` at the repo root is the fallback location
    — marketplaces created before the ``.claude-plugin/`` convention
    must still work."""
    sources = tmp_path / "sources"
    sources.mkdir()
    mkt = _make_marketplace_repo(
        sources,
        "m-old",
        plugins=[],
        use_dot_dir=False,
    )
    catalog = fetch_catalog(_file_url(mkt))
    assert catalog.name == "m-old"


def test_fetch_catalog_malformed_json_raises(tmp_path: Path) -> None:
    """Malformed ``marketplace.json`` surfaces a parse error — we
    don't fall back to treating the marketplace as empty, since that
    would mask broken catalogs and make ``@<m>/<p>`` resolution
    silently fail later."""
    sources = tmp_path / "sources"
    bad = sources / "malformed-mkt"
    bad.mkdir(parents=True)
    _run(["git", "init", "-q", "-b", "main"], cwd=bad)
    _run(["git", "config", "user.email", "t@t"], cwd=bad)
    _run(["git", "config", "user.name", "t"], cwd=bad)
    (bad / ".claude-plugin").mkdir()
    (bad / ".claude-plugin" / "marketplace.json").write_text("not json at all")
    _run(["git", "add", "-A"], cwd=bad)
    _run(["git", "commit", "-q", "-m", "broken catalog"], cwd=bad)

    # Catches Pydantic ValidationError or json.JSONDecodeError —
    # either is acceptable depending on which layer rejects first.
    from pydantic import ValidationError

    with pytest.raises((ValidationError, ValueError)):
        fetch_catalog(_file_url(bad))


def test_fetch_catalog_missing_file_raises(tmp_path: Path) -> None:
    """A repo without either marketplace.json path is rejected — we
    don't want to silently treat a random git repo as an empty
    marketplace."""
    sources = tmp_path / "sources"
    bare = sources / "no-catalog"
    bare.mkdir(parents=True)
    _run(["git", "init", "-q", "-b", "main"], cwd=bare)
    _run(["git", "config", "user.email", "t@t"], cwd=bare)
    _run(["git", "config", "user.name", "t"], cwd=bare)
    (bare / "README.md").write_text("just a readme")
    _run(["git", "add", "-A"], cwd=bare)
    _run(["git", "commit", "-q", "-m", "init"], cwd=bare)

    with pytest.raises(ValueError, match="No marketplace.json"):
        fetch_catalog(_file_url(bare))


# ── add / remove / refresh ─────────────────────────────────────────


def test_add_marketplace_records_catalog(tmp_path: Path) -> None:
    sources = tmp_path / "sources"
    sources.mkdir()
    mkt = _make_marketplace_repo(
        sources,
        "m1",
        plugins=[
            {"name": "alpha", "source": "https://x/alpha"},
            {"name": "beta", "source": "https://x/beta"},
        ],
    )

    entry = add_marketplace(_file_url(mkt), data_dir=tmp_path / "ember")
    assert entry.name == "m1"
    assert entry.cached is not None
    assert {p.name for p in entry.cached.plugins} == {"alpha", "beta"}
    assert entry.last_fetched is not None
    # And it persists.
    persisted = load_registry(data_dir=tmp_path / "ember").find("m1")
    assert persisted is not None
    assert {p.name for p in persisted.cached.plugins} == {"alpha", "beta"}


def test_add_marketplace_replaces_existing_url(tmp_path: Path) -> None:
    """Re-adding a marketplace by the same name updates its URL and
    re-caches — supports migration when a marketplace moves git host
    without renaming itself."""
    sources1 = tmp_path / "sources1"
    sources2 = tmp_path / "sources2"
    sources1.mkdir()
    sources2.mkdir()
    mkt1 = _make_marketplace_repo(sources1, "m1", plugins=[])
    add_marketplace(_file_url(mkt1), data_dir=tmp_path / "ember")

    # Different git URL, same marketplace name, different plugin list.
    mkt2 = _make_marketplace_repo(
        sources2,
        "m1",
        plugins=[
            {"name": "x", "source": "https://x/x"},
        ],
    )
    entry = add_marketplace(_file_url(mkt2), data_dir=tmp_path / "ember")

    assert entry.url == _file_url(mkt2)
    registry = load_registry(data_dir=tmp_path / "ember")
    assert len(registry.marketplaces) == 1
    assert registry.marketplaces[0].cached.plugins[0].name == "x"


def test_remove_marketplace(tmp_path: Path) -> None:
    sources = tmp_path / "sources"
    sources.mkdir()
    mkt = _make_marketplace_repo(sources, "m1", plugins=[])
    add_marketplace(_file_url(mkt), data_dir=tmp_path / "ember")

    assert remove_marketplace("m1", data_dir=tmp_path / "ember") is True
    assert remove_marketplace("m1", data_dir=tmp_path / "ember") is False
    assert load_registry(data_dir=tmp_path / "ember").find("m1") is None


def test_refresh_marketplace_picks_up_new_plugins(tmp_path: Path) -> None:
    """After adding a marketplace and committing a new plugin entry
    upstream, ``refresh_marketplace`` re-pulls the catalog and the
    new entry shows up. Without this, the cached catalog would go
    stale forever."""
    sources = tmp_path / "sources"
    sources.mkdir()
    mkt = _make_marketplace_repo(
        sources,
        "m1",
        plugins=[
            {"name": "alpha", "source": "https://x/alpha"},
        ],
    )
    add_marketplace(_file_url(mkt), data_dir=tmp_path / "ember")

    # Add a new plugin upstream.
    (mkt / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps(
            {
                "name": "m1",
                "plugins": [
                    {"name": "alpha", "source": "https://x/alpha"},
                    {"name": "beta", "source": "https://x/beta"},
                ],
            }
        )
    )
    _run(["git", "add", "-A"], cwd=mkt)
    _run(["git", "commit", "-q", "-m", "add beta"], cwd=mkt)

    entry = refresh_marketplace("m1", data_dir=tmp_path / "ember")
    assert entry is not None
    assert {p.name for p in entry.cached.plugins} == {"alpha", "beta"}


def test_refresh_marketplace_unknown_returns_none(tmp_path: Path) -> None:
    assert refresh_marketplace("nope", data_dir=tmp_path / "ember") is None


# ── resolve_install_ref ────────────────────────────────────────────


def test_resolve_install_ref_returns_source(tmp_path: Path) -> None:
    """``@<marketplace>/<plugin>`` resolves to a :class:`ResolvedSource`
    + the full catalog entry. The bare-URL source form normalizes
    to ``kind='url'`` with no subdir."""
    sources = tmp_path / "sources"
    sources.mkdir()
    mkt = _make_marketplace_repo(
        sources,
        "m1",
        plugins=[
            {"name": "alpha", "source": "https://x/alpha", "branch": "release"},
        ],
    )
    add_marketplace(_file_url(mkt), data_dir=tmp_path / "ember")

    resolved = resolve_install_ref("@m1/alpha", data_dir=tmp_path / "ember")
    assert resolved is not None
    rsrc, entry = resolved
    assert rsrc.kind == "url"
    assert rsrc.url == "https://x/alpha"
    assert rsrc.subdir is None
    # branch falls through from the catalog entry when no explicit
    # ref/sha is on the source object — see ``resolved_source``.
    assert rsrc.ref == "release"
    assert entry.branch == "release"


def test_resolve_install_ref_handles_git_subdir_object_form(
    tmp_path: Path,
) -> None:
    """The official Anthropic catalog uses the ``{"source":
    "git-subdir", "url": ..., "path": ..., "sha": ...}`` object
    shape for ~25% of its entries. Resolver must normalize that
    into ``kind='git-subdir'`` with ``subdir=path`` and ``ref=sha``."""
    sources = tmp_path / "sources"
    sources.mkdir()
    mkt = _make_marketplace_repo(
        sources,
        "m1",
        plugins=[
            {
                "name": "beta",
                "source": {
                    "source": "git-subdir",
                    "url": "https://example.com/skills.git",
                    "path": "plugins/beta",
                    "ref": "main",
                    "sha": "abcdef1234567890abcdef1234567890abcdef12",
                },
            },
        ],
    )
    add_marketplace(_file_url(mkt), data_dir=tmp_path / "ember")

    resolved = resolve_install_ref("@m1/beta", data_dir=tmp_path / "ember")
    assert resolved is not None
    rsrc, _ = resolved
    assert rsrc.kind == "git-subdir"
    assert rsrc.url == "https://example.com/skills.git"
    assert rsrc.subdir == "plugins/beta"
    # sha wins over ref when both are present — the exact-commit
    # pin is more reproducible than a moving branch reference.
    assert rsrc.ref == "abcdef1234567890abcdef1234567890abcdef12"


def test_resolve_install_ref_handles_relative_path_source(
    tmp_path: Path,
) -> None:
    """When the catalog entry's ``source`` is ``./plugins/x``, the
    plugin lives *inside* the marketplace's own git repo. Resolver
    must point the installer back at the marketplace URL with the
    relative path as the subdir."""
    sources = tmp_path / "sources"
    sources.mkdir()
    mkt = _make_marketplace_repo(
        sources,
        "m1",
        plugins=[
            {"name": "gamma", "source": "./plugins/gamma"},
        ],
    )
    mkt_url = _file_url(mkt)
    add_marketplace(mkt_url, data_dir=tmp_path / "ember")

    resolved = resolve_install_ref("@m1/gamma", data_dir=tmp_path / "ember")
    assert resolved is not None
    rsrc, _ = resolved
    assert rsrc.kind == "relative"
    # URL points at the marketplace repo itself — clone it, then
    # descend into the relative path.
    assert rsrc.url == mkt_url
    assert rsrc.subdir == "plugins/gamma"


def test_resolve_install_ref_handles_url_object_form(tmp_path: Path) -> None:
    """The ``{"source": "url", "url": ..., "sha": ...}`` object
    form (used by ~50% of the official catalog) normalizes to
    ``kind='url'``."""
    sources = tmp_path / "sources"
    sources.mkdir()
    mkt = _make_marketplace_repo(
        sources,
        "m1",
        plugins=[
            {
                "name": "delta",
                "source": {
                    "source": "url",
                    "url": "https://example.com/delta.git",
                    "sha": "1234567890abcdef1234567890abcdef12345678",
                },
            },
        ],
    )
    add_marketplace(_file_url(mkt), data_dir=tmp_path / "ember")

    resolved = resolve_install_ref("@m1/delta", data_dir=tmp_path / "ember")
    assert resolved is not None
    rsrc, _ = resolved
    assert rsrc.kind == "url"
    assert rsrc.url == "https://example.com/delta.git"
    assert rsrc.subdir is None
    assert rsrc.ref == "1234567890abcdef1234567890abcdef12345678"


def test_resolve_install_ref_unknown_returns_none(tmp_path: Path) -> None:
    """Unknown marketplace or plugin returns ``None`` — the caller
    falls through to treating the ref as a literal git URL."""
    assert resolve_install_ref("@m1/alpha", data_dir=tmp_path / "ember") is None


def test_resolve_install_ref_non_marketplace_form(tmp_path: Path) -> None:
    """Refs without ``@<m>/<p>`` shape are not marketplace refs —
    return ``None`` so direct git URLs flow through unchanged."""
    assert resolve_install_ref("https://x/y.git", data_dir=tmp_path / "ember") is None
    assert resolve_install_ref("@malformed", data_dir=tmp_path / "ember") is None


# ── End-to-end: marketplace install ────────────────────────────────


def test_marketplace_install_end_to_end(tmp_path: Path) -> None:
    """The full chain: register a marketplace whose plugin source is
    a local plugin repo, then install via ``@m/p`` — should land at
    ``plugins/<name>/`` with a pin, same as URL-install."""
    sources = tmp_path / "sources"
    sources.mkdir()
    plugin_repo = _make_plugin_repo(sources, "gadget")
    mkt = _make_marketplace_repo(
        sources,
        "m1",
        plugins=[
            {"name": "gadget", "source": _file_url(plugin_repo)},
        ],
    )

    data_dir = tmp_path / "ember"
    add_marketplace(_file_url(mkt), data_dir=data_dir)

    resolved = resolve_install_ref("@m1/gadget", data_dir=data_dir)
    assert resolved is not None
    rsrc, _ = resolved

    installer = PluginInstaller(data_dir=data_dir)
    manifest = installer.install(rsrc.url, ref=rsrc.ref, subdir=rsrc.subdir)
    assert manifest.name == "gadget"
    assert (data_dir / "plugins" / "gadget" / ".claude-plugin" / "plugin.json").is_file()

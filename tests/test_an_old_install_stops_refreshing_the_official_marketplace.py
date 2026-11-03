"""An install from before B22 stops cloning the official marketplace at start.

B22 made Anthropic's plugin marketplace opt-in, so a new install never
registers it. An install that had already run kept it in
``marketplaces.json`` (the old default registered it at every session
start), and session start refreshed every registered marketplace: a
``git clone`` from github.com every six hours, nobody having asked (B29).

Session start now leaves the official marketplace alone unless the person
added it themselves or turned ``plugins.default_marketplace`` on. It stays
listed with its cached catalog, and ``/plugin marketplace refresh`` still
fetches it on request. These run the real session-start phase on a real
registry file, with git refusing to start.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from ember_code.core.config.schemas import PluginsConfig, StorageConfig
from ember_code.core.config.settings import Settings
from ember_code.core.plugins.marketplace_store import DEFAULT_MARKETPLACES
from ember_code.core.plugins.models import MarketplaceCatalog, MarketplaceEntry, MarketplaceRegistry
from ember_code.core.session.core import Session
from ember_code.core.session.startup.marketplace import MarketplaceWarmupPhase

[(OFFICIAL_NAME, OFFICIAL_URL)] = DEFAULT_MARKETPLACES
STALE = "2020-01-01T00:00:00+00:00"


def _registry(data_dir: Path, *entries: dict) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    catalog = {"name": "x", "plugins": []}
    (data_dir / "marketplaces.json").write_text(
        json.dumps(
            {"marketplaces": [{"last_fetched": STALE, "cached": catalog, **e} for e in entries]}
        )
    )


def _clones_at_start(data_dir: Path, **plugins: bool) -> list[str]:
    """Run session start's marketplace phase; return the URLs git was asked to clone."""
    cloned: list[str] = []

    def no_git(args, **kwargs) -> subprocess.CompletedProcess:
        cloned.append(next(a for a in args if "://" in a or a.startswith("/")))
        raise subprocess.CalledProcessError(128, args, "", "refused by the test")

    settings = Settings(
        storage=StorageConfig(data_dir=str(data_dir)), plugins=PluginsConfig(**plugins)
    )
    session = Session.__new__(Session)
    session.settings = settings
    with patch("ember_code.core.plugins.git.subprocess.run", no_git):
        asyncio.run(MarketplaceWarmupPhase(session)._refresh_all())
    return cloned


def test_the_old_defaults_entry_is_not_refreshed(tmp_path):
    data = tmp_path / "data"
    _registry(data, {"name": OFFICIAL_NAME, "url": OFFICIAL_URL})  # no added_by: pre-B29
    assert _clones_at_start(data) == []
    # Still listed, with its catalog: nothing the person had is removed.
    [entry] = MarketplaceRegistry.model_validate_json(
        (data / "marketplaces.json").read_text()
    ).marketplaces
    assert entry.name == OFFICIAL_NAME and entry.cached is not None


def test_one_the_person_added_is_refreshed(tmp_path):
    data = tmp_path / "data"
    _registry(data, {"name": OFFICIAL_NAME, "url": OFFICIAL_URL, "added_by": "user"})
    assert _clones_at_start(data) == [OFFICIAL_URL]


def test_opting_in_refreshes_it(tmp_path):
    data = tmp_path / "data"
    _registry(data, {"name": OFFICIAL_NAME, "url": OFFICIAL_URL})
    assert _clones_at_start(data, default_marketplace=True) == [OFFICIAL_URL]


def test_other_marketplaces_are_refreshed_as_before(tmp_path):
    data = tmp_path / "data"
    other = "https://git.acme.test/plugins"
    _registry(
        data,
        {"name": OFFICIAL_NAME, "url": OFFICIAL_URL},
        {"name": "acme", "url": other},
    )
    assert _clones_at_start(data) == [other]


@pytest.mark.parametrize(
    ("before", "again", "after"),
    [
        (None, "user", "user"),
        (None, "default", "default"),
        ("user", "default", "user"),
        ("default", "user", "user"),
    ],
)
def test_who_added_it_is_kept_across_a_re_add(before, again, after):
    """A person's add sticks; an auto-registration never downgrades it."""
    catalog = MarketplaceCatalog(name=OFFICIAL_NAME, plugins=[])
    registry = MarketplaceRegistry(
        marketplaces=[MarketplaceEntry(name=OFFICIAL_NAME, url=OFFICIAL_URL, added_by=before)]
    )
    readded = MarketplaceEntry(name=OFFICIAL_NAME, url=OFFICIAL_URL, added_by=again)
    readded.mark_fetched(catalog)
    assert registry.add(readded).added_by == after


def test_marketplace_add_records_the_person(tmp_path):
    """``/plugin marketplace add`` goes through the store's ``add``, which
    marks the entry as the person's unless told otherwise."""
    from ember_code.core.plugins.marketplace_store import MarketplaceRegistryStore

    store = MarketplaceRegistryStore(tmp_path / "data")
    catalog = MarketplaceCatalog(name=OFFICIAL_NAME, plugins=[])
    with patch.object(store._fetcher, "fetch") as fetch:
        fetch.return_value.unwrap.return_value = catalog
        assert store.add(OFFICIAL_URL).added_by == "user"
        assert store.add(OFFICIAL_URL, added_by="default").added_by == "user"

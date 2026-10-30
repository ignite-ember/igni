"""igni is one product: the core never asks where it runs, and never imports the cloud.

``core/layer.py``. The desktop igni is the core; the cloud is a layer
on top, in its own package (``packages/igni-cloud``), found through
entry points. These hold the core to that.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from ember_code.core.db.providers import SharedStorages, SqliteStorage, StorageUnavailable
from ember_code.core.db.urls import Dialect
from ember_code.core.layer import BackendHooks, Layer, Layers, UnknownLayer

SRC = Path(__file__).resolve().parents[1] / "src" / "ember_code"


def _modules() -> list[tuple[str, ast.Module, str]]:
    found = []
    for path in sorted(SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        found.append((path.relative_to(SRC).as_posix(), ast.parse(text), text))
    return found


def test_the_core_never_imports_the_cloud_package():
    imports = []
    for name, tree, _ in _modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                continue
            imports += [f"{name}: {m}" for m in modules if m.split(".")[0] == "igni_cloud"]
    assert imports == [], "the core finds the cloud through entry points only:\n" + "\n".join(
        imports
    )


def test_only_the_layer_loader_reads_the_profile():
    """``IGNI_PROFILE`` is read in one place, which installs a layer; nothing else branches on it."""
    readers = []
    for name, _, text in _modules():
        if name == "core/layer.py":
            continue
        for marker in (r"IGNI_PROFILE", r"Layers\.ENV", r"RuntimeProfile", r"\.is_cloud\b"):
            if re.search(marker, text):
                readers.append(f"{name}: {marker}")
    assert readers == [], (
        "ask the layer's seams (core/layer.py), not where igni runs:\n" + "\n".join(readers)
    )


def test_unset_the_desktop_runs_and_its_hooks_do_nothing(monkeypatch):
    monkeypatch.delenv(Layers.ENV, raising=False)
    Layers.reset()
    layer = Layers.current()
    assert type(layer) is Layer and layer.log_files() and layer.reads_local_settings()
    assert layer.credentials() is None and layer.mcp_approvals() is None
    assert not layer.requires_database()
    assert type(layer.backend()) is BackendHooks


def test_a_layer_no_package_declares_is_refused_not_ignored(monkeypatch):
    monkeypatch.setenv(Layers.ENV, "nowhere")
    Layers.reset()
    with pytest.raises(UnknownLayer, match="nowhere"):
        Layers.current()


def test_postgresql_without_its_package_says_what_to_install(monkeypatch):
    monkeypatch.setattr("ember_code.core.db.providers.entry_points", lambda group: [])
    monkeypatch.setattr(SharedStorages, "_found", {})
    with pytest.raises(StorageUnavailable, match="igni-cloud"):
        SharedStorages.for_dialect(Dialect.POSTGRESQL)
    assert isinstance(SharedStorages.for_dialect(Dialect.SQLITE), SqliteStorage)

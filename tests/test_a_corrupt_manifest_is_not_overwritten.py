"""A manifest we could not read must never be saved over.

``ManifestStore.load()`` answers a missing *or* unreadable file with a fresh
empty state, and ``mutate()`` is load-mutate-save. So one ``touch()`` after a
truncated write turned the file into ``{"head": null, "commits": []}`` — the
head sha every session prompt reads (``prompt_builder`` does
``Manifest(...).load().head``), the commit list retention walks, and the branch
pins that exempt commits from retention, all gone, with one WARNING line
saying "starting fresh".

Reproduced by execution before the fix: write a real manifest, corrupt the
bytes, call ``touch()``, read the file back — it was the empty state.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ember_code.core.code_index.manifest import ManifestStore


@pytest.fixture
def store(tmp_path: Path) -> ManifestStore:
    project = tmp_path / "proj"
    project.mkdir()
    return ManifestStore(project=project, data_dir=tmp_path / "data")


def _populate(store: ManifestStore) -> None:
    store.upsert_commit("deadbeef", branch_refs=["main"])
    store.set_head("deadbeef")


def test_a_healthy_manifest_still_round_trips(store: ManifestStore) -> None:
    _populate(store)
    store.touch("deadbeef")
    assert store.load().head == "deadbeef"
    assert "deadbeef" in store.load().commits


def test_a_mutation_does_not_write_an_empty_state_over_corrupt_bytes(
    store: ManifestStore,
) -> None:
    _populate(store)
    store.path.write_text("{not json")

    store.touch("deadbeef")

    # Whatever the new file says, the unreadable bytes survive somewhere.
    quarantined = list(store.path.parent.glob("manifest.json.corrupt.*"))
    assert quarantined, "the corrupt manifest was overwritten instead of preserved"
    assert quarantined[0].read_text() == "{not json"


def test_the_write_is_refused_when_the_bytes_cannot_be_preserved(
    store: ManifestStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable file is often a transient I/O failure, not garbage.

    If we cannot move it aside, we must not write in its place — the next read
    may well succeed and find the real manifest.
    """
    _populate(store)
    before = store.path.read_text()
    store.path.write_text("{not json")

    def refuse(*args: object, **kwargs: object) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr("ember_code.core.code_index.manifest.os.replace", refuse)

    with pytest.raises(OSError):
        store.touch("deadbeef")

    assert store.path.read_text() == "{not json"
    assert "deadbeef" in before

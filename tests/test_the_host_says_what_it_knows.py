"""The producers: what actually makes a workspace signal happen.

`workspace.head_changed` is the signal that matters: it is why
CodeIndex polled `git rev-parse` once a second, and an extension
platform whose whole pitch is "stop writing this" should not make
every extension write it. The four signals that predate the host
object model keep their old topic strings as wire names
(`docs/HOST_OBJECT_MODEL.md`, *The existing events*).

Two rules are under test here, and the second is the one that is
easy to get wrong:

1. **A first observation is not a change.** A watcher that
   announces what it has just learned tells every newly installed
   extension that HEAD "moved" to where it has been all along.

2. **The grant is not the content.** `permissions.workspace` says an
   extension may hear `workspace.files_changed`. It does not say
   which files. Handing the unfiltered list to an extension granted
   `docs/**` tells it every path in the repository — and the grant
   check passed, so nothing looked wrong.
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from ember_code.backend.workspace_signals import WorkspaceSignals
from ember_code.backend.workspace_watch import (
    FileStamp,
    GitWorkspace,
    Head,
    WorkspaceWatcher,
)
from ember_code.extensions.events import HostTopic
from ember_code.extensions.host.definition import HOST_SCHEMA
from ember_code.extensions.manifest import ExtensionManifest
from tests.host_model_helpers import HostRig, Sink

FILES = HostTopic.WORKSPACE_FILES_CHANGED.value
HEAD = HostTopic.WORKSPACE_HEAD_CHANGED.value


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[HostRig]:
    rig = await HostRig(tmp_path).start()
    try:
        yield rig
    finally:
        await rig.stop()


def listening(rig: HostRig, extension_id: str, *signals: str) -> Sink:
    """`extension_id`'s process, subscribed to `signals`."""
    assert rig.model is not None
    sink = Sink()
    rig.model.bus.subscribe(rig.model.bus.attach(extension_id, sink), list(signals))
    return sink


def producer(rig: HostRig) -> WorkspaceSignals:
    assert rig.model is not None
    signals = WorkspaceSignals(project_dir=rig.project)
    signals.attach_model(rig.model)
    return signals


# ── A fake workspace, so the rules are testable without a repo ─────


class FakeWorkspace:
    """A `GitWorkspace` whose answers the test sets."""

    def __init__(self) -> None:
        self.head_value: Head | None = Head(commit_sha="aaa", branch="main")
        self.dirty_value: dict[str, FileStamp] | None = {}
        self.head_calls = 0
        self.dirty_calls = 0

    def head(self) -> Head | None:
        self.head_calls += 1
        return self.head_value

    def dirty(self) -> dict[str, FileStamp] | None:
        self.dirty_calls += 1
        return self.dirty_value


def watcher(
    workspace: FakeWorkspace,
    *,
    wants: bool | set[HostTopic] = True,
) -> tuple[WorkspaceWatcher, list[Head], list[frozenset[str]]]:
    heads: list[Head] = []
    files: list[frozenset[str]] = []

    def wanted(topic: HostTopic) -> bool:
        if isinstance(wants, bool):
            return wants
        return topic in wants

    return (
        WorkspaceWatcher(
            workspace=workspace,  # type: ignore[arg-type]
            on_head=heads.append,
            on_files=files.append,
            wants=wanted,
            files_interval_seconds=0.0,
        ),
        heads,
        files,
    )


# ── Rule 1: a first observation is not a change ────────────────────


@pytest.mark.asyncio
async def test_the_first_poll_learns_head_and_says_nothing():
    """Otherwise every extension is told HEAD "moved" to where it
    has been since before it was installed."""
    workspace = FakeWorkspace()
    watch, heads, _ = watcher(workspace)
    await watch.poll_once()
    assert heads == []
    assert workspace.head_calls == 1


@pytest.mark.asyncio
async def test_head_moving_fires_once():
    workspace = FakeWorkspace()
    watch, heads, _ = watcher(workspace)
    await watch.poll_once()
    workspace.head_value = Head(commit_sha="bbb", branch="feat/x")
    await watch.poll_once()
    await watch.poll_once()
    assert [head.commit_sha for head in heads] == ["bbb"]


@pytest.mark.asyncio
async def test_a_branch_switch_to_the_same_commit_still_fires():
    """`git checkout -b` moves the branch and not the sha. An
    extension that renders the branch name needs to know."""
    workspace = FakeWorkspace()
    watch, heads, _ = watcher(workspace)
    await watch.poll_once()
    workspace.head_value = Head(commit_sha="aaa", branch="feat/x")
    await watch.poll_once()
    assert [head.branch for head in heads] == ["feat/x"]


@pytest.mark.asyncio
async def test_nobody_listening_means_no_git_at_all():
    """The cost of an unused event should be zero, not one
    subprocess per second for the life of the backend."""
    workspace = FakeWorkspace()
    watch, heads, files = watcher(workspace, wants=False)
    await watch.poll_once()
    await watch.poll_once()
    assert workspace.head_calls == 0
    assert workspace.dirty_calls == 0
    assert heads == [] and files == []


@pytest.mark.asyncio
async def test_a_listener_arriving_late_re_learns_rather_than_is_told():
    """HEAD moved three times while nobody was listening. The
    extension that just installed is about to read HEAD itself;
    telling it that it changed is noise that looks like news."""
    workspace = FakeWorkspace()
    listening = {HostTopic.WORKSPACE_HEAD_CHANGED}
    watch, heads, _ = watcher(workspace, wants=listening)
    await watch.poll_once()  # baseline at aaa

    listening.clear()
    workspace.head_value = Head(commit_sha="bbb", branch="main")
    await watch.poll_once()  # nobody home; baseline dropped

    listening.add(HostTopic.WORKSPACE_HEAD_CHANGED)
    await watch.poll_once()  # re-baseline at bbb, silently
    assert heads == []

    workspace.head_value = Head(commit_sha="ccc", branch="main")
    await watch.poll_once()
    assert [head.commit_sha for head in heads] == ["ccc"]


# ── Files: membership is not enough ────────────────────────────────


@pytest.mark.asyncio
async def test_a_file_edited_twice_is_reported_twice():
    """The bug a set comparison would have: a dirty file that stays
    dirty looks unchanged, so the second edit is swallowed and an
    extension's cache is stale until something else happens to
    move."""
    workspace = FakeWorkspace()
    watch, _, files = watcher(workspace)
    workspace.dirty_value = {}
    await watch.poll_once()

    workspace.dirty_value = {"a.py": FileStamp(mtime_ns=1, size=10)}
    await watch.poll_once()
    workspace.dirty_value = {"a.py": FileStamp(mtime_ns=2, size=12)}
    await watch.poll_once()

    assert files == [frozenset({"a.py"}), frozenset({"a.py"})]


@pytest.mark.asyncio
async def test_a_file_committed_away_is_reported_as_changed():
    """It left the dirty set because it landed. Something that was
    being watched is now different on disk from what the extension
    last read."""
    workspace = FakeWorkspace()
    watch, _, files = watcher(workspace)
    workspace.dirty_value = {"a.py": FileStamp(mtime_ns=1, size=10)}
    await watch.poll_once()
    workspace.dirty_value = {}
    await watch.poll_once()
    assert files == [frozenset({"a.py"})]


@pytest.mark.asyncio
async def test_an_unchanged_dirty_set_says_nothing():
    workspace = FakeWorkspace()
    watch, _, files = watcher(workspace)
    workspace.dirty_value = {"a.py": FileStamp(mtime_ns=1, size=10)}
    await watch.poll_once()
    await watch.poll_once()
    assert files == []


# ── Rule 2: the grant is not the content ───────────────────────────


async def test_files_changed_is_filtered_to_each_listener(rig: HostRig):
    docs = rig.install("acme.docs", workspace="read", workspacePaths=["docs/*"])
    code = rig.install("acme.code", workspace="read", workspacePaths=["src/*"])
    heard_docs, heard_code = listening(rig, docs, FILES), listening(rig, code, FILES)

    producer(rig).files_changed(frozenset({"docs/a.md", "src/b.py"}))

    assert heard_docs.values(FILES) == [{"paths": ["docs/a.md"], "truncated": False}]
    assert heard_code.values(FILES) == [{"paths": ["src/b.py"], "truncated": False}]


async def test_nothing_i_can_see_moved_is_a_skip_not_an_empty_signal(rig: HostRig):
    """A signal an extension has to ignore is still one it had to be
    given — which is the poll we just removed, arriving from the
    other end."""
    docs = rig.install("acme.docs", workspace="read", workspacePaths=["docs/*"])
    heard = listening(rig, docs, FILES)
    producer(rig).files_changed(frozenset({"src/b.py"}))
    assert heard.got == []


async def test_no_workspace_grant_hears_no_workspace_signal(rig: HostRig):
    """Granted every host object, and not the workspace. A path list is
    the contents of a directory this extension was never told the
    location of."""
    everything = [p.name for p in HOST_SCHEMA.permissions if p.name != "workspace"]
    nosy = rig.install("acme.nosy", workspace="none", host=everything)
    heard = listening(rig, nosy, FILES, HEAD)
    signals = producer(rig)
    signals.files_changed(frozenset({"src/b.py"}))
    signals.head_changed(Head(commit_sha="aaa", branch="feat/acquisition"))
    assert heard.got == []


async def test_head_changed_needs_the_workspace_too(rig: HostRig):
    """A branch name is workspace information. `feat/acquisition-of-x`
    is the example that makes it obvious."""
    seeing = listening(rig, rig.install("acme.sees", workspace="read"), HEAD)
    blind = listening(rig, rig.install("acme.blind", workspace="none"), HEAD)
    producer(rig).head_changed(Head(commit_sha="aaa", branch="main"))
    assert seeing.values(HEAD) == [{"commit_sha": "aaa", "branch": "main"}]
    assert blind.got == []


async def test_a_long_list_is_capped_and_says_so(rig: HostRig):
    """A branch switch can dirty tens of thousands of paths. An
    extension handed all of them has been handed a directory
    listing; `truncated` is its cue to rescan instead."""
    heard = listening(rig, rig.install("acme.wide", workspace="read"), FILES)
    paths = frozenset(f"src/f{n}.py" for n in range(WorkspaceSignals.MAX_PATHS + 50))
    producer(rig).files_changed(paths)
    [value] = heard.values(FILES)
    assert value["truncated"] is True
    assert len(value["paths"]) == WorkspaceSignals.MAX_PATHS


async def test_the_poll_runs_only_while_someone_entitled_listens(rig: HostRig):
    """The cost of an unused signal is zero: no subscriber with the
    workspace, no git."""
    signals = producer(rig)
    blind = rig.install("acme.blind", workspace="none")
    listening(rig, blind, HEAD)
    assert not signals._wanted(HostTopic.WORKSPACE_HEAD_CHANGED)  # noqa: SLF001
    listening(rig, rig.install("acme.sees", workspace="read"), HEAD)
    assert signals._wanted(HostTopic.WORKSPACE_HEAD_CHANGED)  # noqa: SLF001
    assert not signals._wanted(HostTopic.WORKSPACE_FILES_CHANGED)  # noqa: SLF001


# ── The old grant is gone ──────────────────────────────────────────


def test_a_manifest_declaring_the_retired_event_grant_does_not_validate():
    """`permissions.events` duplicated `permissions.host` and was
    retired. A manifest that still declares it is refused, rather than
    approved and then delivered nothing."""
    with pytest.raises(ValidationError, match="events"):
        ExtensionManifest.model_validate(
            {
                "id": "acme.old",
                "version": "1.0.0",
                "contract": ">=1.0 <2",
                "trust": "unsigned",
                "dev": {"command": ["true"]},
                "permissions": {"events": ["workspace.head-changed"], "workspace": "read"},
            }
        )


# ── The real thing ─────────────────────────────────────────────────


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "T")
    (tmp_path / ".gitignore").write_text("ignored/\n")
    (tmp_path / "a.py").write_text("x\n")
    git("add", "-A")
    git("commit", "-m", "first")
    return tmp_path


def test_git_workspace_reads_head(repo: Path):
    head = GitWorkspace(repo).head()
    assert head is not None
    assert head.branch == "main"
    assert len(head.commit_sha) == 40


def test_git_workspace_reports_edits_and_untracked(repo: Path):
    (repo / "a.py").write_text("y\n")
    (repo / "new.txt").write_text("hi\n")
    dirty = GitWorkspace(repo).dirty()
    assert dirty is not None
    assert set(dirty) == {"a.py", "new.txt"}


def test_git_workspace_skips_ignored_files(repo: Path):
    """The reason this is built on git and not on a filesystem
    watcher: a watcher has to learn `.gitignore` before it is
    usable, and reports every write inside `node_modules` until it
    does."""
    (repo / "ignored").mkdir()
    (repo / "ignored" / "junk.bin").write_text("x\n")
    dirty = GitWorkspace(repo).dirty()
    assert dirty == {}


def test_git_workspace_keeps_a_deleted_path(repo: Path):
    """git lists it and `stat` cannot. Dropping it would make the
    delete invisible the moment it left the set."""
    (repo / "a.py").unlink()
    dirty = GitWorkspace(repo).dirty()
    assert dirty is not None
    assert dirty["a.py"] == FileStamp(mtime_ns=0, size=0)


def test_a_rename_arrives_as_two_paths(repo: Path):
    """`--no-renames`, so an extension is told what to re-read and
    what to forget rather than being handed the one record format
    that carries two paths."""
    subprocess.run(["git", "mv", "a.py", "b.py"], cwd=repo, check=True, capture_output=True)
    dirty = GitWorkspace(repo).dirty()
    assert dirty is not None
    assert set(dirty) == {"a.py", "b.py"}


def test_not_a_repository_is_a_state_not_a_failure(tmp_path: Path):
    """A watcher runs in an ordinary directory all the time. It
    should produce no events, not an exception once a second."""
    workspace = GitWorkspace(tmp_path / "plain")
    (tmp_path / "plain").mkdir()
    assert workspace.head() is None
    assert workspace.dirty() is None


# ── Cadence ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_files_are_polled_less_often_than_head():
    """`git rev-parse` is microseconds; `git status` walks the
    index. Running both at 1 Hz would make the cheap event pay for
    the expensive one on every repository big enough to care."""
    workspace = FakeWorkspace()
    watch, _, _ = watcher(workspace)
    watch._files_interval = 60.0  # noqa: SLF001 — the cadence is the subject
    await watch.poll_once()
    await watch.poll_once()
    await watch.poll_once()
    assert workspace.head_calls == 3
    assert workspace.dirty_calls == 1


@pytest.mark.asyncio
async def test_the_loop_polls_and_stops():
    """`stop` has to cancel an in-flight poll, not just stop
    scheduling the next one — the same hole that left
    `ExtensionHealth` hanging for ten minutes."""
    workspace = FakeWorkspace()
    watch, heads, _ = watcher(workspace)
    watch._head_interval = 0.01  # noqa: SLF001
    await watch.start()
    # Let it baseline at `aaa` first — the loop is subject to the
    # same "a first observation is not a change" rule as everything
    # else, and a test that moves HEAD before the first tick is
    # testing that rule rather than the loop.
    for _ in range(100):
        await asyncio.sleep(0.01)
        if workspace.head_calls:
            break
    workspace.head_value = Head(commit_sha="bbb", branch="main")
    for _ in range(100):
        await asyncio.sleep(0.01)
        if heads:
            break
    await watch.stop()
    assert [head.commit_sha for head in heads] == ["bbb"]
    settled = workspace.head_calls
    await asyncio.sleep(0.05)
    assert workspace.head_calls == settled

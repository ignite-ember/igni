"""The producers: what actually makes a host event happen.

The transport was finished before any of it fired. The router knew
who was entitled, the push channel carried an addressed envelope,
and the frame had an `onEvent` — and the only event in the system
was `extension.state-changed`, because the registry was the one
thing the host already watched. `workspace.head-changed` is the one
that matters: it is why CodeIndex polls `git rev-parse` once a
second, and an extension platform whose whole pitch is "stop
writing this" should not make every extension write it.

Two rules are under test here, and the second is the one that is
easy to get wrong:

1. **A first observation is not a change.** A watcher that
   announces what it has just learned tells every newly installed
   extension that HEAD "moved" to where it has been all along.

2. **The grant is not the content.** `permissions.events` says an
   extension may be told about `workspace.files-changed`. It does
   not say which files. Handing the unfiltered list to an extension
   granted `docs/**` tells it every path in the repository — and
   the grant check passed, so nothing looked wrong.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from ember_code.backend.host_events import HostEvents
from ember_code.backend.workspace_watch import (
    FileStamp,
    GitWorkspace,
    Head,
    WorkspaceWatcher,
)
from ember_code.extensions.events import EventRouter, HostTopic
from ember_code.extensions.installer import ExtensionInstaller
from ember_code.extensions.manifest import ExtensionManifest
from ember_code.extensions.registry import ExtensionRegistry


def manifest(
    extension_id: str,
    *,
    events: list[str],
    workspace: str = "read",
    paths: list[str] | None = None,
) -> ExtensionManifest:
    return ExtensionManifest.model_validate(
        {
            "id": extension_id,
            "version": "1.0.0",
            "contract": ">=1.0 <2",
            "trust": "unsigned",
            "dev": {"command": ["true"]},
            "permissions": {
                "events": events,
                "workspace": workspace,
                "workspacePaths": paths or [],
            },
        }
    )


class Heard:
    """Every event one extension was handed, in order."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def __call__(self, topic: str, payload: dict) -> None:
        self.events.append((topic, payload))

    def topics(self) -> list[str]:
        return [topic for topic, _ in self.events]


def wired(*installed: ExtensionManifest) -> tuple[HostEvents, dict[str, Heard]]:
    """A `HostEvents` over a registry, with every frame listening."""
    registry = ExtensionRegistry()
    for one in installed:
        registry.install(one)
    router = EventRouter(registry)
    heard = {}
    for one in installed:
        heard[one.id] = Heard()
        router.connect(one.id, heard[one.id])
    return HostEvents(router=router, project_dir=Path("/nonexistent")), heard


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


def test_files_changed_is_filtered_to_each_listener():
    docs = manifest("acme.docs", events=["workspace.*"], paths=["docs/*"])
    code = manifest("acme.code", events=["workspace.*"], paths=["src/*"])
    host, heard = wired(docs, code)

    host.files_changed(frozenset({"docs/a.md", "src/b.py"}))

    assert heard["acme.docs"].events == [
        ("workspace.files-changed", {"paths": ["docs/a.md"], "truncated": False})
    ]
    assert heard["acme.code"].events == [
        ("workspace.files-changed", {"paths": ["src/b.py"], "truncated": False})
    ]


def test_nothing_i_can_see_moved_is_a_skip_not_an_empty_event():
    """An event an extension has to ignore is still an event it had
    to be given — which is the poll we just removed, arriving from
    the other end."""
    docs = manifest("acme.docs", events=["workspace.*"], paths=["docs/*"])
    host, heard = wired(docs)
    host.files_changed(frozenset({"src/b.py"}))
    assert heard["acme.docs"].events == []


def test_no_workspace_grant_hears_no_workspace_event():
    """The topic was granted and the workspace was not. A path list
    is the contents of a directory this extension was never told
    the location of."""
    nosy = manifest("acme.nosy", events=["workspace.*"], workspace="none")
    host, heard = wired(nosy)
    host.files_changed(frozenset({"src/b.py"}))
    host.head_changed(Head(commit_sha="aaa", branch="feat/acquisition"))
    assert heard["acme.nosy"].events == []


def test_head_changed_needs_the_workspace_too():
    """A branch name is workspace information. `feat/acquisition-of-x`
    is the example that makes it obvious."""
    seeing = manifest("acme.sees", events=["workspace.head-changed"])
    blind = manifest("acme.blind", events=["workspace.head-changed"], workspace="none")
    host, heard = wired(seeing, blind)
    host.head_changed(Head(commit_sha="aaa", branch="main"))
    assert heard["acme.sees"].topics() == ["workspace.head-changed"]
    assert heard["acme.blind"].topics() == []


def test_a_long_list_is_capped_and_says_so():
    """A branch switch can dirty tens of thousands of paths. An
    extension handed all of them has been handed a directory
    listing; `truncated` is its cue to rescan instead."""
    wide = manifest("acme.wide", events=["workspace.*"])
    host, heard = wired(wide)
    host.files_changed(frozenset(f"src/f{n}.py" for n in range(HostEvents.MAX_PATHS + 50)))
    ((_, payload),) = heard["acme.wide"].events
    assert payload["truncated"] is True
    assert len(payload["paths"]) == HostEvents.MAX_PATHS


def test_an_ungranted_topic_is_still_not_heard():
    """The content rule is extra, not instead of. Workspace access
    does not grant a topic nobody asked for."""
    quiet = manifest("acme.quiet", events=[])
    host, heard = wired(quiet)
    host.files_changed(frozenset({"src/b.py"}))
    assert heard["acme.quiet"].events == []


# ── Session ────────────────────────────────────────────────────────


def test_a_reload_is_not_a_session_switch():
    """`attach_session` is what a client sends when it reconnects.
    Without the dedupe every page refresh looks like the user moved
    to a different session, and every extension re-fetches."""
    one = manifest("acme.one", events=["session.switched"])
    host, heard = wired(one)
    host.session_switched("s1")
    host.session_switched("s1")
    host.session_switched("s2")
    assert [payload["session_id"] for _, payload in heard["acme.one"].events] == ["s1", "s2"]


def test_session_switched_does_not_need_the_workspace():
    """The content rule is about workspace events. A session id is
    not workspace information, and gating it on `workspace` would
    be a rule applied past where it means anything."""
    one = manifest("acme.one", events=["session.switched"], workspace="none")
    host, heard = wired(one)
    host.session_switched("s1")
    assert heard["acme.one"].topics() == ["session.switched"]


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


# ── What the author is told at install ─────────────────────────────


def test_a_workspace_event_without_the_workspace_is_called_out(caplog):
    """The author's only other evidence is an event that never
    arrives. A grant that reads like a subscription and behaves
    like silence is the worst shape a permission can have."""
    ExtensionInstaller._check_events(
        manifest("acme.quiet", events=["workspace.head-changed"], workspace="none")
    )
    assert "was not granted the workspace" in caplog.text


def test_a_topic_igni_does_not_publish_is_called_out(caplog):
    ExtensionInstaller._check_events(manifest("acme.future", events=["session.renamed"]))
    assert "events igni does not publish" in caplog.text
    assert "session.renamed" in caplog.text


def test_a_well_formed_manifest_is_told_nothing(caplog):
    ExtensionInstaller._check_events(
        manifest("acme.good", events=["workspace.*"], workspace="read")
    )
    assert caplog.text == ""

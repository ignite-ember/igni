"""A working copy knows which document project it belongs to, through remote changes.

Search used to rank every document the user could open, so two
repositories' pages came back mixed. Every write and search now carries
the working copy's project, found by remote and remembered in the
clone's git config — which is what lets a changed remote be recognised
as the same project.

These drive real git repositories and a fake server that applies the
same rules as igni__artifacts' resolver.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import httpx
import pytest

from ember_code.core.artifacts.collaboration import CollaborationSync
from ember_code.core.artifacts.project import PIN_FILE, PIN_KEY, WorkingCopyProject

SERVER = "https://igni.example"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "api"
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "remote", "add", "origin", "git@github.com:acme/api.git")
    return path


def _normalize(remote: str) -> str:
    text = remote.strip().removesuffix("/").removesuffix(".git")
    if "://" in text:
        text = text.split("://", 1)[1].split("@")[-1]
    else:
        text = text.split("@")[-1].replace(":", "/", 1)
    return text.lower()


class FakeServer:
    """igni__artifacts' project rules, in memory."""

    def __init__(self) -> None:
        self.projects: dict[str, str] = {}  # id -> name
        self.remotes: dict[str, str] = {}  # remote key -> project id
        self.calls = 0

    async def resolve(self, *, remote, remembered_project_id, name=""):
        self.calls += 1
        key = _normalize(remote) if remote else None
        remembered = remembered_project_id if remembered_project_id in self.projects else None
        if key and key in self.remotes:
            owner = self.remotes[key]
            if remembered and remembered != owner:
                return self._body(remembered, "conflict", key, conflicting=owner)
            return self._body(owner, "found", key)
        if remembered:
            if key:
                self.remotes[key] = remembered
                return self._body(remembered, "attached", key)
            return self._body(remembered, "found", key)
        project_id = f"p{len(self.projects) + 1}"
        self.projects[project_id] = name
        if key:
            self.remotes[key] = project_id
        return self._body(project_id, "created", key)

    def _body(self, project_id, status, key, conflicting=""):
        return {
            "project_id": project_id,
            "name": self.projects[project_id],
            "status": status,
            "remotes": sorted(k for k, v in self.remotes.items() if v == project_id),
            "remote_key": key or "",
            "conflicting_project_id": conflicting,
            "conflicting_name": self.projects.get(conflicting, ""),
        }


@pytest.fixture
def server() -> FakeServer:
    return FakeServer()


class TestResolving:
    async def test_the_first_call_creates_the_project_and_pins_it_in_the_clone(self, repo, server):
        project = WorkingCopyProject(repo, server.resolve)

        assert await project.project_id() == "p1"
        assert _git(repo, "config", "--local", "--get", PIN_KEY) == "p1"
        assert "Started the document project 'api'" in project.notice

    async def test_an_unchanged_working_copy_is_not_asked_again(self, repo, server):
        project = WorkingCopyProject(repo, server.resolve)
        for _ in range(5):
            await project.project_id()

        assert server.calls == 1, "nothing changed, so nothing should be re-resolved"

    async def test_a_second_clone_of_the_same_repo_lands_in_the_same_project(
        self, repo, server, tmp_path
    ):
        await WorkingCopyProject(repo, server.resolve).project_id()
        other = tmp_path / "other-clone"
        other.mkdir()
        _git(other, "init", "-q")
        _git(other, "remote", "add", "origin", "https://github.com/acme/api")

        assert await WorkingCopyProject(other, server.resolve).project_id() == "p1"


class TestTheRemoteChanges:
    async def test_a_changed_remote_is_noticed_on_the_next_call_and_stays_in_the_project(
        self, repo, server
    ):
        project = WorkingCopyProject(repo, server.resolve)
        assert await project.project_id() == "p1"

        _git(repo, "remote", "set-url", "origin", "git@github.com:acme/platform-api.git")

        assert await project.project_id() == "p1"
        assert server.remotes["github.com/acme/platform-api"] == "p1"
        assert server.remotes["github.com/acme/api"] == "p1", "the old remote keeps working"
        assert "now github.com/acme/platform-api" in project.notice

    async def test_a_fresh_clone_of_the_new_url_finds_it(self, repo, server, tmp_path):
        project = WorkingCopyProject(repo, server.resolve)
        await project.project_id()
        _git(repo, "remote", "set-url", "origin", "git@gitlab.com:acme/api.git")
        await project.project_id()

        fresh = tmp_path / "fresh"
        fresh.mkdir()
        _git(fresh, "init", "-q")
        _git(fresh, "remote", "add", "origin", "https://gitlab.com/acme/api.git")

        assert await WorkingCopyProject(fresh, server.resolve).project_id() == "p1"

    async def test_a_remote_owned_by_another_project_is_reported_not_followed(
        self, repo, server, tmp_path
    ):
        web = tmp_path / "web"
        web.mkdir()
        _git(web, "init", "-q")
        _git(web, "remote", "add", "origin", "git@github.com:acme/web.git")
        assert await WorkingCopyProject(web, server.resolve).project_id() == "p1"

        project = WorkingCopyProject(repo, server.resolve)
        assert await project.project_id() == "p2"
        _git(repo, "remote", "set-url", "origin", "git@github.com:acme/web.git")

        assert await project.project_id() == "p2", "kept, not switched"
        assert "belongs to the document project" in project.notice


class TestNoGit:
    async def test_a_plain_folder_remembers_its_project_in_igni(self, tmp_path, server):
        folder = tmp_path / "notes"
        folder.mkdir()
        project = WorkingCopyProject(folder, server.resolve)

        first = await project.project_id()

        assert first == "p1"
        assert (folder / PIN_FILE).read_text().strip() == "p1"
        assert await WorkingCopyProject(folder, server.resolve).project_id() == "p1"


class TestTheServerIsAway:
    async def test_no_answer_is_unscoped_and_not_retried_on_every_call(self, repo):
        calls = []

        async def down(**_):
            calls.append(1)
            raise httpx.ConnectError("refused")

        project = WorkingCopyProject(repo, down)

        assert await project.project_id() is None
        assert await project.project_id() is None
        assert len(calls) == 1


def _sync(handler) -> tuple[CollaborationSync, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    return CollaborationSync(object(), server_url=SERVER, access_token="t", client=client), seen  # type: ignore[arg-type]


class TestEveryCallCarriesTheProject:
    async def test_search_is_scoped(self):
        sync, seen = _sync(lambda r: httpx.Response(200, json={"hits": []}))

        async def provider():
            return "p-api"

        sync.project_provider = provider
        await sync.search(query="release plan")

        assert seen[-1].url.params["project_id"] == "p-api"

    async def test_without_a_project_search_is_unscoped_as_before(self):
        sync, seen = _sync(lambda r: httpx.Response(200, json={"hits": []}))
        await sync.search(query="release plan")

        assert "project_id" not in seen[-1].url.params

    async def test_a_local_path_remote_falls_back_to_what_is_remembered(self):
        def handler(request):
            import json as _json

            body = _json.loads(request.content)
            if body.get("remote"):
                return httpx.Response(400, json={"detail": "local path"})
            return httpx.Response(200, json={"project_id": "p9", "name": "x", "status": "found"})

        sync, seen = _sync(handler)
        answer = await sync.resolve_project(remote="/srv/git/x.git", remembered_project_id="p9")

        assert answer["project_id"] == "p9" and len(seen) == 2

    async def test_a_server_without_projects_leaves_everything_unscoped(self):
        sync, _ = _sync(lambda r: httpx.Response(404, json={"detail": "Not Found"}))

        assert (
            await sync.resolve_project(remote="git@github.com:a/b.git", remembered_project_id=None)
            is None
        )


class TestTheProjectCommand:
    """`/artifacts project` says which project, and fixes a conflict on request."""

    def _session(self, repo, server, linked):
        from types import SimpleNamespace

        class Shared:
            is_configured = True

            async def link_remote(self, *, project_id, remote):
                linked.append((project_id, remote))
                server.remotes[_normalize(remote)] = project_id
                return {"remote_key": _normalize(remote)}

        resolver = WorkingCopyProject(repo, server.resolve)
        return SimpleNamespace(
            artifact_project=resolver, artifacts=SimpleNamespace(_shared=Shared())
        ), resolver

    async def test_it_shows_the_project_and_its_remotes(self, repo, server):
        from ember_code.backend.cmd_artifacts import ArtifactsCommand

        session, _ = self._session(repo, server, [])
        result = await ArtifactsCommand(session).dispatch("project")

        assert "Document project: api (p1)" in result.content
        assert "github.com/acme/api" in result.content

    async def test_link_moves_the_remote_to_this_project(self, repo, server, tmp_path):
        from ember_code.backend.cmd_artifacts import ArtifactsCommand

        web = tmp_path / "web"
        web.mkdir()
        _git(web, "init", "-q")
        _git(web, "remote", "add", "origin", "git@github.com:acme/web.git")
        await WorkingCopyProject(web, server.resolve).project_id()  # p1 owns acme/web

        linked: list = []
        session, resolver = self._session(repo, server, linked)
        assert await resolver.project_id() == "p2"
        _git(repo, "remote", "set-url", "origin", "git@github.com:acme/web.git")

        result = await ArtifactsCommand(session).dispatch("project link")

        assert linked == [("p2", "git@github.com:acme/web.git")]
        assert "now opens 'api'" in result.content
        assert server.remotes["github.com/acme/web"] == "p2"

    async def test_without_a_store_it_says_so(self, repo):
        from types import SimpleNamespace

        from ember_code.backend.cmd_artifacts import ArtifactsCommand

        result = await ArtifactsCommand(
            SimpleNamespace(artifact_project=None, artifacts=None)
        ).dispatch("project")

        assert "No document store" in result.content

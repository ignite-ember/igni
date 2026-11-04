"""An extension is shared the way a plugin is, and can carry one.

Two questions that turned out to be the same question.

**How do you hand somebody an extension you are developing?** A
plugin is a git repository, because that is how people share things.
An extension could only be installed from a directory you already
had, which makes "try my extension" a conversation about zip files.
It takes a repository now — the same two forms `igni plugin install`
takes. Both are development installs, behind ``IGNI_DEV_EXTENSIONS``:
a released extension reaches people from its service through igni
server, which is tested in ``test_extensions_arrive_from_their_services.py``.

**How does an extension ship its agents?** Artifacts has the
archivist, the only agent allowed to touch the document store;
CodeIndex has ten variant files. `EXTENSIBILITY.md` says agents,
skills and hooks are not contribution types because plugins
already carry them — and then a capability extracted into an
extension lost its agents on the way out. The answer is not a
second path: an extension *wraps* a plugin, igni installs it with
the extension and removes it with it, and the plugin loader that
was already there picks it up.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from ember_code.cli.ext import ext
from ember_code.core.mcp.client import MCPClientManager
from ember_code.extensions.installer import ExtensionInstaller
from ember_code.extensions.manifest import ExtensionManifest
from ember_code.extensions.origin import ExtensionOrigins
from ember_code.extensions.registry import ExtensionRegistry, ExtensionState
from ember_code.extensions.rpc import ExtensionRpc

FIXTURE = Path(__file__).parent / "fixtures" / "dev_extension"


@pytest.fixture(autouse=True)
def dev_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every install here is a development install."""
    monkeypatch.setenv("IGNI_DEV_EXTENSIONS", "1")


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.email=t@x", "-c", "user.name=T", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> dict[str, object]:
    """A bundle in a repository, with two commits and a tag.

    Two, so "the latest" and "that tag" are different answers — a
    single-commit fixture would pass whichever one the code picked.
    """
    root = tmp_path / "acme-ext"
    shutil.copytree(FIXTURE, root)
    _git(root, "init", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "v1")
    first = _git(root, "rev-parse", "HEAD")
    _git(root, "tag", "v1.0.0")

    manifest = json.loads((root / "manifest.json").read_text())
    manifest["version"] = "2.0.0"
    (root / "manifest.json").write_text(json.dumps(manifest))
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "v2")
    return {
        "url": f"file://{root}",
        "path": root,
        "first": first,
        "second": _git(root, "rev-parse", "HEAD"),
    }


def _install(repo: dict[str, object], root: Path, *extra: str):
    return CliRunner().invoke(ext, ["install", str(repo["url"]), "--root", str(root), *extra])


def _installed_at(output: str) -> str:
    return next(
        (line.strip()[3:] for line in output.splitlines() if line.strip().startswith("at ")),
        "",
    )


class TestInstallingFromARepository:
    def test_the_latest_is_a_choice_somebody_can_make(self, repo, tmp_path):
        """No ref is the default branch as it stands, and that is a
        legitimate answer rather than something to be warned about."""
        done = _install(repo, tmp_path / "d")
        assert done.exit_code == 0, done.output
        assert _installed_at(done.output) == repo["second"]

    @pytest.mark.parametrize("ref_key,label", [("first", "v1.0.0")])
    def test_a_tag_installs_that_tag(self, repo, tmp_path, ref_key, label):
        done = _install(repo, tmp_path / "d", "--ref", label)
        assert done.exit_code == 0, done.output
        assert _installed_at(done.output) == repo[ref_key]

    def test_a_commit_installs_that_commit(self, repo, tmp_path):
        """The one that did not work first time. A `--depth 1` clone
        holds only the tip, so checking out an older commit failed
        with "unable to read tree" — which reads like a corrupt
        repository rather than like the depth it is."""
        done = _install(repo, tmp_path / "d", "--ref", repo["first"])
        assert done.exit_code == 0, done.output
        assert _installed_at(done.output) == repo["first"]

    def test_a_short_commit_works_too(self, repo, tmp_path):
        """Because it is what somebody pastes."""
        done = _install(repo, tmp_path / "d", "--ref", str(repo["first"])[:12])
        assert done.exit_code == 0, done.output
        assert _installed_at(done.output) == repo["first"]

    def test_what_was_installed_is_always_said(self, repo, tmp_path):
        """A fact, not advice. Whether to pin is the installer's
        call; this is the commit it meant today."""
        done = _install(repo, tmp_path / "d")
        assert len(_installed_at(done.output)) == 40

    def test_a_directory_still_installs(self, repo, tmp_path):
        """An air-gapped deployment clones nothing."""
        done = CliRunner().invoke(
            ext, ["install", str(repo["path"]), "--root", str(tmp_path / "d")]
        )
        assert done.exit_code == 0, done.output

    def test_a_ref_against_a_directory_is_refused(self, repo, tmp_path):
        """Rather than accepted and ignored, which is how somebody
        believes they pinned something."""
        done = CliRunner().invoke(
            ext,
            ["install", str(repo["path"]), "--root", str(tmp_path / "d"), "--ref", "v1.0.0"],
        )
        assert done.exit_code != 0
        assert "is a directory" in done.output

    def test_neither_a_directory_nor_a_url_says_which(self, tmp_path):
        done = CliRunner().invoke(ext, ["install", "not-a-thing", "--root", str(tmp_path / "d")])
        assert done.exit_code != 0
        assert "does not look like a repository URL" in done.output


class TestCarryingAPlugin:
    """`contributes.plugin` — how an extension ships its agents."""

    def test_the_manifest_can_name_one(self):
        manifest = ExtensionManifest.model_validate(
            {
                "id": "acme.scanner",
                "version": "1.0.0",
                "contract": ">=1.0 <2",
                "trust": "unsigned",
                "dev": {"command": ["true"]},
                "contributes": {"plugin": "plugin"},
            }
        )
        assert manifest.contributes.plugin == "plugin"

    def test_it_is_absent_by_default(self):
        """Most extensions have no agents, and an empty directory
        copied into the plugins folder is a plugin the loader warns
        about."""
        manifest = ExtensionManifest.model_validate(
            {
                "id": "acme.scanner",
                "version": "1.0.0",
                "contract": ">=1.0 <2",
                "trust": "unsigned",
                "dev": {"command": ["true"]},
            }
        )
        assert manifest.contributes.plugin is None


class TestThePluginActuallyArrives:
    """Through the real installer, into a real plugins directory."""

    @pytest.fixture
    def bundle(self, tmp_path: Path) -> Path:
        root = tmp_path / "ext"
        shutil.copytree(FIXTURE, root)
        manifest = json.loads((root / "manifest.json").read_text())
        manifest["dev"]["command"] = [sys.executable, "server.py"]
        manifest.setdefault("contributes", {})["plugin"] = "plugin"
        (root / "manifest.json").write_text(json.dumps(manifest))

        plugin = root / "plugin"
        (plugin / ".claude-plugin").mkdir(parents=True)
        (plugin / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "acme-scanner", "version": "1.0.0"})
        )
        (plugin / "agents").mkdir()
        (plugin / "agents" / "auditor.md").write_text(
            "---\nname: auditor\ndescription: Reads the scanner's findings.\n---\nYou audit.\n"
        )
        return root

    async def _install(self, bundle: Path, tmp_path: Path, plugins_dir: Path | None):
        registry = ExtensionRegistry()
        rpc = ExtensionRpc(registry)
        mcp = MCPClientManager(project_dir=tmp_path)
        origins = ExtensionOrigins()
        installer = ExtensionInstaller(
            registry=registry,
            rpc=rpc,
            mcp=mcp,
            origins=origins,
            allow_development=True,
            plugins_dir=plugins_dir,
        )
        try:
            entry = await installer.install(bundle)
            yield installer, entry
        finally:
            await origins.stop_all()
            await mcp.disconnect_all()

    @pytest.mark.asyncio
    async def test_the_agent_lands_where_the_loader_looks(self, bundle, tmp_path):
        plugins = tmp_path / "plugins"
        async for _installer, entry in self._install(bundle, tmp_path, plugins):
            assert entry.state is ExtensionState.READY, entry.reason
            agent = plugins / "acme.scanner" / "agents" / "auditor.md"
            assert agent.is_file(), sorted(p.name for p in plugins.rglob("*"))

    @pytest.mark.asyncio
    async def test_it_is_filed_under_the_extension_not_the_plugin(self, bundle, tmp_path):
        """Uninstall has to find it again, and a plugin free to call
        itself anything is one that gets left behind — or collides
        with one somebody installed by hand."""
        plugins = tmp_path / "plugins"
        async for _installer, _entry in self._install(bundle, tmp_path, plugins):
            assert (plugins / "acme.scanner").is_dir()
            assert not (plugins / "acme-scanner").exists()

    @pytest.mark.asyncio
    async def test_uninstalling_takes_the_agent_with_it(self, bundle, tmp_path):
        """An agent left behind is worse than one that never came:
        every tool it has fails, and it sits in the same list as the
        ones that work."""
        plugins = tmp_path / "plugins"
        async for installer, _entry in self._install(bundle, tmp_path, plugins):
            assert (plugins / "acme.scanner").is_dir()
            assert await installer.uninstall("acme.scanner") is True
            assert not (plugins / "acme.scanner").exists()

    @pytest.mark.asyncio
    async def test_a_deployment_with_nowhere_to_put_it_still_installs(
        self, bundle, tmp_path, caplog
    ):
        """Degraded, and said so. Refusing the install would be
        worse: the service works and only the agents are missing."""
        async for _installer, entry in self._install(bundle, tmp_path, None):
            assert entry.state is ExtensionState.READY, entry.reason
            assert "no plugins directory" in caplog.text

    @pytest.mark.asyncio
    async def test_a_directory_outside_the_bundle_is_refused(self, bundle, tmp_path, caplog):
        """The path comes from a manifest, and `../../..` is a
        manifest asking igni to copy somebody else's directory."""
        manifest = json.loads((bundle / "manifest.json").read_text())
        manifest["contributes"]["plugin"] = "../elsewhere"
        (bundle / "manifest.json").write_text(json.dumps(manifest))
        plugins = tmp_path / "plugins"
        async for _installer, entry in self._install(bundle, tmp_path, plugins):
            assert entry.state is ExtensionState.READY
            assert "not in its bundle" in caplog.text
            assert not plugins.exists() or not any(plugins.iterdir())

    @pytest.mark.asyncio
    async def test_a_directory_without_a_plugin_manifest_is_refused(self, bundle, tmp_path, caplog):
        """The loader skips a directory with no
        `.claude-plugin/plugin.json` silently, so copying one there
        would be an agent that never appears and no line saying why."""
        shutil.rmtree(bundle / "plugin" / ".claude-plugin")
        plugins = tmp_path / "plugins"
        async for _installer, entry in self._install(bundle, tmp_path, plugins):
            assert entry.state is ExtensionState.READY
            assert "plugin.json" in caplog.text


class TestOneRepositoryCanHoldSeveral:
    """`--subdir`, the same way plugins do it.

    A plugin repository is often a parent of many — the official
    marketplace's `git-subdir` source is about half its catalog —
    and there is no reason an extension repository should be
    different. It is not, and this says so rather than leaving it
    to be discovered.

    What an extension does *not* have is a marketplace. These are
    installable but not browsable: you have to be told the subdir.
    """

    @pytest.fixture
    def monorepo(self, tmp_path: Path) -> str:
        root = tmp_path / "monorepo"
        for name, ident in (("scanner", "acme.scanner"), ("auditor", "acme.auditor")):
            where = root / "extensions" / name
            shutil.copytree(FIXTURE, where)
            manifest = json.loads((where / "manifest.json").read_text())
            manifest["id"] = ident
            (where / "manifest.json").write_text(json.dumps(manifest))
        _git(root, "init", "-b", "main")
        _git(root, "add", "-A")
        _git(root, "commit", "-m", "two extensions")
        return f"file://{root}"

    def test_each_one_installs_from_its_own_subdir(self, monorepo, tmp_path):
        store = tmp_path / "store"
        for name, ident in (("scanner", "acme.scanner"), ("auditor", "acme.auditor")):
            done = CliRunner().invoke(
                ext,
                ["install", monorepo, "--root", str(store), "--subdir", f"extensions/{name}"],
            )
            assert done.exit_code == 0, done.output
            assert ident in done.output
        assert sorted(p.name for p in store.iterdir()) == ["acme.auditor", "acme.scanner"]

    def test_a_subdir_that_is_not_there_says_so(self, monorepo, tmp_path):
        """Rather than installing the repository root, which would
        be a bundle with no manifest and a confusing error."""
        done = CliRunner().invoke(
            ext,
            ["install", monorepo, "--root", str(tmp_path / "s"), "--subdir", "extensions/nope"],
        )
        assert done.exit_code != 0
        assert "not a directory" in done.output

    def test_two_bundles_with_one_id_collide(self, tmp_path):
        """The store keys by extension id, not by where it came
        from — so a monorepo's ids are a namespace its author has
        to manage, and the second install refuses rather than
        overwriting the first."""
        root = tmp_path / "clash"
        for name in ("one", "two"):
            shutil.copytree(FIXTURE, root / name)
        _git(root, "init", "-b", "main")
        _git(root, "add", "-A")
        _git(root, "commit", "-m", "same id twice")

        store = tmp_path / "store"
        first = CliRunner().invoke(
            ext, ["install", f"file://{root}", "--root", str(store), "--subdir", "one"]
        )
        assert first.exit_code == 0, first.output
        second = CliRunner().invoke(
            ext, ["install", f"file://{root}", "--root", str(store), "--subdir", "two"]
        )
        assert second.exit_code != 0
        assert "already" in second.output.lower()

"""``igni ext`` — the commands EXTENSIBILITY §9 had been promising.

``igni ext init`` and ``igni ext doctor`` were in that document for
as long as there was an extension system, and neither existed.
Installing meant calling ``ExtensionInstaller`` from Python, which is
not something an administrator does and not something a partner can
be handed.

The scaffold test runs the whole loop — init, doctor, install, and
then starting the result through the real installer against a real
subprocess. A scaffold that needs edits before it works is a
scaffold that teaches nothing about whether the install path is
sound, so what ``init`` writes has to run as it is.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from ember_code.cli.ext import ext
from ember_code.core.mcp.client import MCPClientManager
from ember_code.extensions.installer import ExtensionInstaller
from ember_code.extensions.origin import ExtensionOrigins
from ember_code.extensions.registry import ExtensionRegistry, ExtensionState
from ember_code.extensions.rpc import ExtensionRpc
from ember_code.extensions.store import ExtensionStore, StoreError

FIXTURE = Path(__file__).parent / "fixtures" / "dev_extension"


@pytest.fixture
def run(tmp_path: Path):
    """Invoke ``igni ext`` with a store under ``tmp_path``."""
    runner = CliRunner()
    store = tmp_path / "store"

    def invoke(*args: str, rooted: bool = True):
        argv = [*args, "--root", str(store)] if rooted else list(args)
        return runner.invoke(ext, argv, catch_exceptions=False)

    invoke.store = store  # type: ignore[attr-defined]
    return invoke


# ── init ───────────────────────────────────────────────────────────


def test_init_writes_something_doctor_is_happy_with(run, tmp_path):
    """The first command after `init` must not fail.

    It did: an unsigned development bundle was reported as a problem,
    so `doctor` failed on the output of `init` — the very first thing
    an author saw was the tooling contradicting itself.
    """
    target = tmp_path / "mine"
    assert run("init", str(target), rooted=False).exit_code == 0

    result = run("doctor", str(target), rooted=False)
    assert result.exit_code == 0, result.output
    assert "no problems found" in result.output
    # Still told, because it matters before anybody else installs it.
    assert "unsigned" in result.output


def test_init_refuses_a_directory_with_something_in_it(run, tmp_path):
    target = tmp_path / "mine"
    target.mkdir()
    (target / "keepme.txt").write_text("mine")
    assert run("init", str(target), rooted=False).exit_code != 0
    assert (target / "keepme.txt").read_text() == "mine"


def test_the_scaffold_has_every_file_it_needs(run, tmp_path):
    target = tmp_path / "mine"
    run("init", str(target), rooted=False)
    for name in ("manifest.json", "server.py", "README.md", "ui/index.html"):
        assert (target / name).is_file(), name


def test_the_scaffold_grants_itself_only_its_own_namespace(run, tmp_path):
    target = tmp_path / "mine"
    run("init", str(target), rooted=False)
    manifest = json.loads((target / "manifest.json").read_text())
    assert manifest["permissions"]["rpc"] == [f"ext.{manifest['id']}.*"]


# ── doctor ─────────────────────────────────────────────────────────


def test_doctor_prints_the_contract_this_igni_implements(run, tmp_path):
    """The thing an author needs before anything else: what range to
    declare."""
    target = tmp_path / "mine"
    run("init", str(target), rooted=False)
    result = run("doctor", str(target), rooted=False)
    assert "contract implemented by this igni" in result.output


def test_doctor_refuses_a_directory_that_is_not_an_extension(run, tmp_path):
    empty = tmp_path / "nothing"
    empty.mkdir()
    result = run("doctor", str(empty), rooted=False)
    assert result.exit_code != 0
    assert "no manifest.json" in result.output


def test_doctor_names_a_sidecar_as_not_startable(run, tmp_path):
    bundle = tmp_path / "side"
    bundle.mkdir()
    (bundle / "manifest.json").write_text(
        json.dumps(
            {
                "id": "acme.scanner",
                "version": "1.0.0",
                "contract": ">=1.0 <2",
                "trust": "partner",
                "signature": "x",
                "sidecar": {
                    "image": "acme__scanner:1.0.0",
                    "mcp": {"transport": "stdio"},
                },
            }
        )
    )
    result = run("doctor", str(bundle), rooted=False)
    assert result.exit_code != 0
    assert "container images" in result.output


def test_doctor_notices_pages_with_no_ui(run, tmp_path):
    bundle = tmp_path / "nopage"
    bundle.mkdir()
    (bundle / "manifest.json").write_text(
        json.dumps(
            {
                "id": "acme.scanner",
                "version": "1.0.0",
                "contract": ">=1.0 <2",
                "trust": "unsigned",
                "dev": {"command": ["python", "server.py"]},
                "contributes": {"pages": [{"kind": "acme.scan", "label": "Scan"}]},
            }
        )
    )
    result = run("doctor", str(bundle), rooted=False)
    assert result.exit_code != 0
    assert "no 'ui' directory" in result.output


def test_doctor_writes_nothing(run, tmp_path):
    """Safe to run first, and safe to run on somebody else's
    directory."""
    target = tmp_path / "mine"
    run("init", str(target), rooted=False)
    before = {p: p.stat().st_mtime_ns for p in sorted(target.rglob("*"))}
    run("doctor", str(target), rooted=False)
    after = {p: p.stat().st_mtime_ns for p in sorted(target.rglob("*"))}
    assert before == after


# ── install / list / remove ────────────────────────────────────────


def test_install_copies_the_bundle_and_says_what_it_granted(run, tmp_path):
    """The grants are printed because that is the moment an
    administrator is deciding."""
    target = tmp_path / "mine"
    run("init", str(target), rooted=False)
    result = run("install", str(target))

    assert result.exit_code == 0, result.output
    assert "it may use" in result.output
    assert "backend methods" in result.output
    assert (run.store / "local.mine" / "manifest.json").is_file()


def test_installing_twice_needs_replace(run, tmp_path):
    """An upgrade re-asks for permissions, which is a separate
    decision from the first install."""
    target = tmp_path / "mine"
    run("init", str(target), rooted=False)
    assert run("install", str(target)).exit_code == 0

    second = run("install", str(target))
    assert second.exit_code != 0
    assert "--replace" in second.output

    assert run("install", str(target), "--replace").exit_code == 0


def test_the_source_is_copied_not_referenced(run, tmp_path):
    """A bundle in somebody's Downloads is one `rm` from a deployment
    that cannot start its extensions."""
    target = tmp_path / "mine"
    run("init", str(target), rooted=False)
    run("install", str(target))

    import shutil

    shutil.rmtree(target)
    assert (run.store / "local.mine" / "server.py").is_file()


def test_list_and_remove(run, tmp_path):
    target = tmp_path / "mine"
    run("init", str(target), rooted=False)
    run("install", str(target))

    assert "local.mine" in run("list").output

    removed = run("remove", "local.mine")
    assert removed.exit_code == 0
    assert "local.mine" not in run("list").output

    assert run("remove", "local.mine").exit_code != 0


def test_list_on_an_empty_deployment_says_so(run):
    assert "nothing installed" in run("list").output


# ── The store ──────────────────────────────────────────────────────


def test_a_directory_with_no_manifest_is_skipped_not_fatal(tmp_path):
    """One corrupt extension must not stop a backend from starting
    the others."""
    store = ExtensionStore(tmp_path)
    (tmp_path / "junk").mkdir()
    good = tmp_path / "acme.scanner"
    good.mkdir()
    (good / "manifest.json").write_text((FIXTURE / "manifest.json").read_text())
    assert [p.name for p in store.installed()] == ["acme.scanner"]


def test_an_interrupted_copy_leaves_nothing_to_start(tmp_path, monkeypatch):
    """Staged and renamed, so a half-written bundle is never visible
    to the boot loader."""
    import shutil as shutil_module

    store = ExtensionStore(tmp_path / "store")

    def explode(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(shutil_module, "copytree", explode)
    with pytest.raises(OSError, match="disk full"):
        store.add(FIXTURE)
    assert store.installed() == []


def test_a_silly_id_cannot_escape_the_store(tmp_path):
    store = ExtensionStore(tmp_path)
    for bad in ("../escape", "a/b", "."):
        with pytest.raises(StoreError):
            store.path_for(bad)


# ── The whole loop ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_what_init_writes_actually_runs(run, tmp_path):
    """Scaffold, install, start, call.

    The point of the scaffold is that it works as written. If it
    needed edits first, it would prove nothing about whether the
    install path is sound — and this is the only test that covers
    `init`, `ExtensionStore` and `ExtensionInstaller` as one thing.
    """
    target = tmp_path / "mine"
    assert run("init", str(target), rooted=False).exit_code == 0
    assert run("install", str(target)).exit_code == 0

    store = ExtensionStore(run.store)
    (bundle,) = store.installed()

    # The scaffold says "python"; point it at the interpreter running
    # these tests, which is the one with the SDK.
    manifest = json.loads((bundle / "manifest.json").read_text())
    manifest["dev"]["command"] = [sys.executable, "server.py"]
    (bundle / "manifest.json").write_text(json.dumps(manifest))

    registry = ExtensionRegistry()
    rpc = ExtensionRpc(registry)
    mcp = MCPClientManager(project_dir=tmp_path)
    origins = ExtensionOrigins()
    installer = ExtensionInstaller(
        registry=registry, rpc=rpc, mcp=mcp, origins=origins, allow_unsigned=True
    )
    try:
        entry = await installer.install(bundle)
        assert entry.state is ExtensionState.READY, entry.reason
        assert origins.url_for("local.mine")

        answer = await rpc.dispatch(caller="local.mine", method="ext.local.mine.hello", args={})
        assert answer == {"message": "hello from the extension backend"}
    finally:
        await origins.stop_all()
        await mcp.disconnect_all()

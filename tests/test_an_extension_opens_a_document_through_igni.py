"""``host.workspace.open``: igni opens a document for an extension that cannot.

An extension's process runs in an OS sandbox that cannot launch an
application (no ``open``, no ``xdg-open``), so it asks igni, which opens
the file with the backend's :class:`~ember_code.backend.file_opener.FileOpener`.
Whatever opens it runs outside the sandbox, so the host opens only a
document the workspace grant reaches, and only with ``files.open``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ember_code.extensions.host import members
from tests.host_model_helpers import HostRig


@pytest.fixture
async def rig(tmp_path: Path):
    rig = await HostRig(tmp_path).start()
    try:
        yield rig
    finally:
        await rig.stop()


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """What igni asked the desktop to open, in place of opening it."""
    seen: list[Path] = []

    def record(path: Path) -> bool:
        seen.append(path)
        return True

    monkeypatch.setattr(members.FileOpener, "open", staticmethod(record))
    return seen


def ok(reply: Any) -> Any:
    assert reply.ok, f"refused: {reply.error}"
    return reply.result


def refused(reply: Any, code: str) -> str:
    assert not reply.ok, f"answered {reply.result!r}"
    assert reply.error.code == code, reply.error
    return reply.error.message


def document(rig: HostRig, relative: str, text: str = "<p>hi</p>") -> Path:
    path = rig.project / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


async def test_a_document_in_the_workspace_is_opened_by_igni(rig: HostRig, opened) -> None:
    ext = rig.install(host=["files.open"], workspace="read")
    path = document(rig, "exports/design-v3.html")

    assert ok(await rig.ask(ext, "call", "workspace.open", path="exports/design-v3.html")) is None

    assert opened == [path.resolve()]


async def test_an_absolute_path_inside_the_workspace_is_the_same(rig: HostRig, opened) -> None:
    ext = rig.install(host=["files.open"], workspace="read")
    path = document(rig, "notes.md", "# notes")

    ok(await rig.ask(ext, "call", "workspace.open", path=str(path)))

    assert opened == [path.resolve()]


async def test_it_needs_files_open(rig: HostRig, opened) -> None:
    ext = rig.install(host=["ui"], workspace="read")
    document(rig, "a.html")

    message = refused(await rig.ask(ext, "call", "workspace.open", path="a.html"), "not_granted")

    assert "files.open" in message
    assert opened == []


async def test_it_needs_the_workspace_too(rig: HostRig, opened) -> None:
    ext = rig.install(host=["files.open"])
    document(rig, "a.html")

    refused(await rig.ask(ext, "call", "workspace.open", path="a.html"), "not_granted")
    assert opened == []


async def test_only_what_the_grant_reaches(rig: HostRig, opened, tmp_path: Path) -> None:
    ext = rig.install(host=["files.open"], workspace="read", workspacePaths=["exports/**"])
    document(rig, "exports/a.html")
    document(rig, "secret.html")
    outside = tmp_path / "elsewhere.html"
    outside.write_text("x")

    ok(await rig.ask(ext, "call", "workspace.open", path="exports/a.html"))
    refused(await rig.ask(ext, "call", "workspace.open", path="secret.html"), "not_granted")
    refused(await rig.ask(ext, "call", "workspace.open", path=str(outside)), "refused")
    refused(await rig.ask(ext, "call", "workspace.open", path="../elsewhere.html"), "refused")

    assert [p.name for p in opened] == ["a.html"]


async def test_a_link_out_of_the_workspace_is_outside_it(rig: HostRig, opened, tmp_path) -> None:
    ext = rig.install(host=["files.open"], workspace="read")
    outside = tmp_path / "elsewhere.html"
    outside.write_text("x")
    (rig.project / "inside.html").symlink_to(outside)

    refused(await rig.ask(ext, "call", "workspace.open", path="inside.html"), "refused")
    assert opened == []


@pytest.mark.parametrize(
    "relative",
    [
        "run.command",
        "install.sh",
        "Tool.app/Contents/Info.plist",
        "a.webloc",
        "x.terminal",
        "noext",
    ],
)
async def test_only_a_document_opens(rig: HostRig, opened, relative: str) -> None:
    ext = rig.install(host=["files.open"], workspace="write")
    document(rig, relative, "#!/bin/sh\n")

    refused(await rig.ask(ext, "call", "workspace.open", path=relative), "invalid")
    assert opened == []


async def test_an_executable_document_does_not_open(rig: HostRig, opened) -> None:
    ext = rig.install(host=["files.open"], workspace="write")
    path = document(rig, "page.html")
    path.chmod(0o755)

    refused(await rig.ask(ext, "call", "workspace.open", path="page.html"), "invalid")
    assert opened == []


async def test_a_missing_file_or_a_directory_is_not_found(rig: HostRig, opened) -> None:
    ext = rig.install(host=["files.open"], workspace="read")
    (rig.project / "exports").mkdir()

    refused(await rig.ask(ext, "call", "workspace.open", path="gone.html"), "not_found")
    refused(await rig.ask(ext, "call", "workspace.open", path="exports"), "not_found")
    assert opened == []


async def test_nothing_opening_it_is_a_failure(rig: HostRig, monkeypatch) -> None:
    monkeypatch.setattr(members.FileOpener, "open", staticmethod(lambda path: False))
    ext = rig.install(host=["files.open"], workspace="read")
    document(rig, "a.pdf", "%PDF")

    refused(await rig.ask(ext, "call", "workspace.open", path="a.pdf"), "failed")

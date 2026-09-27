"""What the Open button is allowed to open.

``artifact_open_export`` takes a path from the UI and hands it to the
operating system. That is the whole RPC, so the tests that matter are
the ones about what it *refuses* — without them the method is "open
any file on this machine, by name", which is not what the button next
to an export says it does.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ember_code.backend.server_artifacts import ArtifactController


def controller(project: Path) -> ArtifactController:
    """A controller whose session is only a project directory."""
    return ArtifactController(SimpleNamespace(project_dir=str(project), artifacts=object()))


@pytest.fixture
def opened(monkeypatch) -> list[Path]:
    """Records what reached the launcher instead of launching it."""
    calls: list[Path] = []

    def fake_open(path: Path) -> bool:
        calls.append(path)
        return True

    monkeypatch.setattr("ember_code.backend.server_artifacts.FileOpener.open", fake_open)
    return calls


class TestWhatItOpens:
    @pytest.mark.asyncio
    async def test_an_exported_file_in_the_project(self, tmp_path: Path, opened):
        target = tmp_path / "auth-v2.pdf"
        target.write_bytes(b"%PDF-1.7\n")

        result = await controller(tmp_path).open_export(str(target))

        assert result.opened is True
        assert result.error == ""
        assert opened == [target.resolve()]

    @pytest.mark.asyncio
    async def test_every_format_export_writes(self, tmp_path: Path, opened):
        for name in ("a.html", "b.md", "c.pdf"):
            (tmp_path / name).write_text("x")
            assert (await controller(tmp_path).open_export(str(tmp_path / name))).opened

    @pytest.mark.asyncio
    async def test_a_launcher_that_fails_is_reported_not_swallowed(
        self, tmp_path: Path, monkeypatch
    ):
        target = tmp_path / "auth-v2.pdf"
        target.write_text("x")
        monkeypatch.setattr("ember_code.backend.server_artifacts.FileOpener.open", lambda _p: False)

        result = await controller(tmp_path).open_export(str(target))

        assert result.opened is False
        assert "Could not open" in result.error


class TestWhatItRefuses:
    @pytest.mark.asyncio
    async def test_a_file_outside_the_project(self, tmp_path: Path, opened):
        # The path arrives over RPC. Nothing about the UI stops a
        # different caller asking for ``~/.ssh/id_rsa.pdf``.
        outside = tmp_path.parent / "elsewhere.pdf"
        outside.write_text("x")
        project = tmp_path / "project"
        project.mkdir()

        result = await controller(project).open_export(str(outside))

        assert result.opened is False
        assert "outside the project" in result.error
        assert opened == []

    @pytest.mark.asyncio
    async def test_an_extension_we_do_not_export(self, tmp_path: Path, opened):
        target = tmp_path / "run.sh"
        target.write_text("#!/bin/sh\nrm -rf /\n")

        result = await controller(tmp_path).open_export(str(target))

        assert result.opened is False
        assert "Only exported documents" in result.error
        assert opened == []

    @pytest.mark.asyncio
    async def test_a_directory_that_looks_like_a_file(self, tmp_path: Path, opened):
        directory = tmp_path / "notes.md"
        directory.mkdir()

        result = await controller(tmp_path).open_export(str(directory))

        assert result.opened is False
        assert opened == []

    @pytest.mark.asyncio
    async def test_a_path_that_is_no_longer_there(self, tmp_path: Path, opened):
        # Exports are files on disk; between the banner appearing and
        # the button being pressed, one can be moved or deleted.
        result = await controller(tmp_path).open_export(str(tmp_path / "gone.pdf"))

        assert result.opened is False
        assert "no longer there" in result.error
        assert opened == []

    @pytest.mark.asyncio
    async def test_a_symlink_pointing_out_of_the_project(self, tmp_path: Path, opened):
        # ``resolve`` before the containment check, not after: a link
        # inside the project whose target is outside it would otherwise
        # pass a check done on the name alone.
        secret = tmp_path / "secret.pdf"
        secret.write_text("x")
        project = tmp_path / "project"
        project.mkdir()
        link = project / "innocent.pdf"
        link.symlink_to(secret)

        result = await controller(project).open_export(str(link))

        assert result.opened is False
        assert "outside the project" in result.error
        assert opened == []

    @pytest.mark.asyncio
    async def test_a_relative_climb_out_of_the_project(self, tmp_path: Path, opened):
        outside = tmp_path / "elsewhere.pdf"
        outside.write_text("x")
        project = tmp_path / "project"
        project.mkdir()

        result = await controller(project).open_export(str(project / ".." / "elsewhere.pdf"))

        assert result.opened is False
        assert "outside the project" in result.error
        assert opened == []

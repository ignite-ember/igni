"""The FE file preview reads two directories and nothing else.

The containment itself was already right, and probing it confirmed that:
relative traversal, deep traversal, an absolute path, ``/etc/passwd``, a
symlink inside the project pointing out of it, ``~/.ssh/id_rsa`` and a NUL
byte were all refused. The order is what makes it work — the requested
path is resolved *before* the comparison, so ``..`` is collapsed and a
symlink has already been followed to wherever it really points.

What was wrong was the *allowlist*. It named ``DEFAULT_DATA_DIR`` — the
constant ``~/.igni`` — rather than asking where the data actually is, and
that constant was wrong in two situations:

* on a machine that has not migrated, ``home_config_dir()`` returns the
  pre-rename ``~/.ember`` and the app reads and writes there, so the
  preview refused the config file the session was using;
* ``storage.data_dir`` is configurable, so a deployment that moved it
  could not preview anything inside it.

Both are "refused something legitimate", not "allowed something it should
not" — but a sandbox that blocks the user's own config is the kind of
thing that gets a sandbox widened carelessly later.
"""

from __future__ import annotations

import inspect
import types
from pathlib import Path
from unittest.mock import patch

import pytest

import ember_code.backend.server_files as server_files

FilesController = next(
    value
    for value in vars(server_files).values()
    if inspect.isclass(value) and hasattr(value, "read_file")
)


@pytest.fixture
def world(tmp_path: Path):
    """A project, a home with only the legacy config dir, a data_dir
    somewhere else, and a secret outside all of them."""
    home = tmp_path / "home"
    project = tmp_path / "proj"
    project.mkdir(parents=True)
    (project / "inside.txt").write_text("INSIDE")

    legacy = home / ".ember"
    legacy.mkdir(parents=True)
    (legacy / "config.yaml").write_text("LEGACY-CONFIG")

    data_dir = tmp_path / "mnt" / "igni"
    data_dir.mkdir(parents=True)
    (data_dir / "config.yaml").write_text("CUSTOM-CONFIG")

    outside = tmp_path / "outside.txt"
    outside.write_text("SECRET-OUTSIDE")
    (project / "escape-link").symlink_to(outside)

    controller = FilesController.__new__(FilesController)
    controller._session = types.SimpleNamespace(
        project_dir=project,
        settings=types.SimpleNamespace(storage=types.SimpleNamespace(data_dir=str(data_dir))),
    )
    return types.SimpleNamespace(
        home=home,
        project=project,
        legacy=legacy,
        data_dir=data_dir,
        outside=outside,
        controller=controller,
    )


def _read(world, path):
    with (
        patch.object(Path, "home", staticmethod(lambda: world.home)),
        patch.dict("os.environ", {"HOME": str(world.home)}),
    ):
        return world.controller.read_file(str(path))


class TestWhatItRefuses:
    """The containment, which was already correct and stays that way.

    Widening the allowlist is only safe because of the resolve-first
    order, so these run against the widened version.
    """

    def test_relative_traversal(self, world):
        assert "SECRET-OUTSIDE" not in (_read(world, "../outside.txt").contents or "")

    def test_deep_traversal(self, world):
        result = _read(world, "../../../../../../etc/passwd")
        assert "root:" not in (result.contents or "")

    def test_an_absolute_path_outside(self, world):
        assert "SECRET-OUTSIDE" not in (_read(world, world.outside).contents or "")

    def test_a_symlink_that_escapes_the_project(self, world):
        """The one a purely lexical check would miss: the path is inside
        the project, and only resolving reveals where it points."""
        result = _read(world, "escape-link")

        assert "SECRET-OUTSIDE" not in (result.contents or "")
        assert "Refused" in (result.error or "")

    def test_escaping_out_of_the_configured_data_dir(self, world):
        """Widening the allowlist must not hand over a new base to
        traverse from."""
        escape = world.data_dir / ".." / ".." / "outside.txt"

        assert "SECRET-OUTSIDE" not in (_read(world, escape).contents or "")

    def test_a_home_shorthand_outside_the_roots(self, world):
        assert not _read(world, "~/.ssh/id_rsa").contents

    def test_a_nul_byte_is_a_bad_path_not_a_crash(self, world):
        result = _read(world, "inside.txt\x00/etc/passwd")

        assert not result.contents
        assert "bad path" in (result.error or "")


class TestWhatItAllows:
    def test_a_file_in_the_project(self, world):
        assert _read(world, "inside.txt").contents == "INSIDE"

    def test_the_config_dir_the_session_actually_uses(self, world):
        """The regression the constant caused. This home has only
        ``.ember``, so ``home_config_dir()`` returns it and the session
        reads its config from there — the preview has to agree."""
        result = _read(world, world.legacy / "config.yaml")

        assert result.contents == "LEGACY-CONFIG", result.error

    def test_a_configured_data_dir_elsewhere(self, world):
        result = _read(world, world.data_dir / "config.yaml")

        assert result.contents == "CUSTOM-CONFIG", result.error


class TestTheRootsThemselves:
    def test_they_are_derived_not_hardcoded(self, world):
        """The property that stops this drifting again: the roots come
        from asking, so a future change to where config lives is picked up
        without anybody remembering this file."""
        with (
            patch.object(Path, "home", staticmethod(lambda: world.home)),
            patch.dict("os.environ", {"HOME": str(world.home)}),
        ):
            roots = world.controller._allowed_roots()

        assert world.project.resolve() in roots
        assert world.legacy.resolve() in roots
        assert world.data_dir.resolve() in roots

    def test_a_session_without_settings_still_works(self, world):
        """Headless and test contexts construct thinner sessions; a
        preview must not fail because ``settings`` is absent."""
        world.controller._session = types.SimpleNamespace(project_dir=world.project)

        assert _read(world, "inside.txt").contents == "INSIDE"

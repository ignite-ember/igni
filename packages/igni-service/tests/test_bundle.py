"""Building an extension bundle: a tiny extension, a pure-Python dependency, no network.

The dependencies live in a local find-links directory and everything is
built without build isolation (hatchling is in this package's dev
environment), so these tests run offline and still go through the real
`uv pip compile`, `uv build` and `pip download` the builder uses.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tarfile
import textwrap
from pathlib import Path

import pytest

from igni_service import BundleError
from igni_service.bundle import BundleBuilder, BundleCommand
from igni_service.registration import ExtensionBundle

PLATFORM_NAMES = ("macOS arm64", "Linux x86_64", "Windows x86_64")


def write_package(root: Path, name: str, *, dependencies: list[str], scripts: str = "") -> Path:
    module = name.replace("-", "_")
    (root / "src" / module).mkdir(parents=True)
    (root / "src" / module / "__init__.py").write_text("def main() -> None:\n    pass\n")
    (root / "pyproject.toml").write_text(
        textwrap.dedent(
            f"""\
            [build-system]
            requires = ["hatchling"]
            build-backend = "hatchling.build"

            [project]
            name = "{name}"
            version = "1.0.0"
            requires-python = ">=3.12"
            dependencies = {json.dumps(dependencies)}

            [tool.hatch.build.targets.wheel]
            packages = ["src/{module}"]
            """
        )
        + scripts
    )
    return root


def build_dist(source: Path, out: Path, kind: str) -> None:
    built = subprocess.run(
        [
            "uv",
            "build",
            kind,
            "--no-build-isolation",
            "--python",
            sys.executable,
            "--out-dir",
            str(out),
            str(source),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert built.returncode == 0, built.stderr


@pytest.fixture
def index(tmp_path: Path) -> Path:
    """A find-links directory: `tinydep` as a wheel, `sdistonly` as an sdist only."""
    index = tmp_path / "index"
    build_dist(write_package(tmp_path / "tinydep", "tinydep", dependencies=[]), index, "--wheel")
    build_dist(
        write_package(tmp_path / "sdistonly", "sdistonly", dependencies=[]), index, "--sdist"
    )
    return index


def write_extension(
    root: Path, *, dependencies: list[str], sdist_only: list[str] | None = None
) -> Path:
    scripts = '\n[project.scripts]\nacme-example = "acme_example:main"\n'
    if sdist_only is not None:
        scripts += f"\n[tool.igni.bundle]\nsdist-only = {json.dumps(sdist_only)}\n"
    write_package(root, "acme-example", dependencies=dependencies, scripts=scripts)
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "id": "acme.example",
                "version": "1.0.0",
                "contract": ">=1.0 <2",
                "name": "Example",
                "python": {
                    "package": "acme-example",
                    "entry": "acme-example",
                    "wheels": "wheels",
                    "mcp": {"transport": "stdio"},
                },
            }
        )
    )
    (root / "README.md").write_text("# Example\n")
    (root / "plugin" / "agents").mkdir(parents=True)
    (root / "plugin" / "agents" / "helper.md").write_text("---\nname: helper\n---\n")
    (root / "ui").mkdir()
    (root / "ui" / "index.html").write_text("<!doctype html>")
    (root / "tests").mkdir()
    (root / "tests" / "test_nothing.py").write_text("")
    return root


def members(archive: Path) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(archive.read_bytes()), mode="r:gz") as tar:
        return sorted(member.name for member in tar.getmembers())


def builder(extension: Path, index: Path) -> BundleBuilder:
    return BundleBuilder(extension, find_links=[index], no_index=True, build_isolation=False)


def test_a_bundle_carries_the_manifest_its_files_and_a_wheelhouse(
    tmp_path: Path, index: Path
) -> None:
    extension = write_extension(tmp_path / "extension", dependencies=["tinydep"])

    report = builder(extension, index).build(tmp_path / "bundle.tar.gz")

    assert members(report.out) == [
        "README.md",
        "manifest.json",
        "plugin/agents/helper.md",
        "ui/index.html",
        "wheels/acme_example-1.0.0-py3-none-any.whl",
        "wheels/tinydep-1.0.0-py3-none-any.whl",
    ]
    assert (report.extension_id, report.version) == ("acme.example", "1.0.0")
    shipped = ExtensionBundle.at(report.out)
    assert shipped is not None
    assert (shipped.extension_id, shipped.version, shipped.sha256) == (
        "acme.example",
        "1.0.0",
        report.sha256,
    )


def test_the_same_inputs_give_the_same_bytes(tmp_path: Path, index: Path) -> None:
    extension = write_extension(tmp_path / "extension", dependencies=["tinydep"])

    first = builder(extension, index).build(tmp_path / "one.tar.gz")
    second = builder(extension, index).build(tmp_path / "two.tar.gz")

    assert first.sha256 == second.sha256


def test_a_dependency_with_no_wheel_stops_the_build_by_name_and_platform(
    tmp_path: Path, index: Path
) -> None:
    extension = write_extension(tmp_path / "extension", dependencies=["tinydep", "sdistonly"])

    with pytest.raises(BundleError) as failed:
        builder(extension, index).build(tmp_path / "bundle.tar.gz")

    message = str(failed.value)
    assert "sdistonly==1.0.0" in message
    assert all(platform in message for platform in PLATFORM_NAMES)
    assert "sdist-only" in message
    assert not (tmp_path / "bundle.tar.gz").exists()


def test_a_declared_sdist_only_dependency_is_built_from_its_sdist(
    tmp_path: Path, index: Path
) -> None:
    extension = write_extension(
        tmp_path / "extension", dependencies=["tinydep", "sdistonly"], sdist_only=["SdistOnly"]
    )

    report = builder(extension, index).build(tmp_path / "bundle.tar.gz")

    assert "wheels/sdistonly-1.0.0-py3-none-any.whl" in members(report.out)
    [note] = report.notes
    assert "sdistonly==1.0.0" in note and "declared sdist-only" in note


def test_the_allowlist_can_be_given_on_the_command_line(
    tmp_path: Path, index: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    extension = write_extension(tmp_path / "extension", dependencies=["sdistonly"])
    out = tmp_path / "bundle.tar.gz"

    status = BundleCommand.main(
        [
            "--extension",
            str(extension),
            "--out",
            str(out),
            "--find-links",
            str(index),
            "--no-index",
            "--no-build-isolation",
            "--allow-sdist",
            "sdistonly",
        ]
    )

    assert status == 0
    assert "wrote" in capsys.readouterr().out
    assert "wheels/sdistonly-1.0.0-py3-none-any.whl" in members(out)


def test_the_command_fails_loudly(
    tmp_path: Path, index: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    extension = write_extension(tmp_path / "extension", dependencies=["sdistonly"])

    status = BundleCommand.main(
        [
            "--extension",
            str(extension),
            "--out",
            str(tmp_path / "b.tar.gz"),
            "--find-links",
            str(index),
            "--no-index",
            "--no-build-isolation",
        ]
    )

    assert status == 1
    assert "sdistonly==1.0.0: no cp312/abi3/pure wheel" in capsys.readouterr().err


def test_a_manifest_that_does_not_match_the_package_is_refused(tmp_path: Path, index: Path) -> None:
    extension = write_extension(tmp_path / "extension", dependencies=[])
    manifest = json.loads((extension / "manifest.json").read_text())
    manifest["python"]["entry"] = "not-declared"
    (extension / "manifest.json").write_text(json.dumps(manifest))

    with pytest.raises(BundleError, match="not-declared"):
        builder(extension, index).build(tmp_path / "bundle.tar.gz")

    manifest["python"] = {"package": "someone-else", "entry": "acme-example"}
    (extension / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(BundleError, match="someone-else"):
        builder(extension, index).build(tmp_path / "bundle.tar.gz")


def test_a_manifest_without_the_python_packaging_is_refused(tmp_path: Path, index: Path) -> None:
    extension = write_extension(tmp_path / "extension", dependencies=[])
    (extension / "manifest.json").write_text(json.dumps({"id": "acme.example", "version": "1.0.0"}))

    with pytest.raises(BundleError, match="python"):
        builder(extension, index).build(tmp_path / "bundle.tar.gz")

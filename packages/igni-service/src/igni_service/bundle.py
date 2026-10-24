"""Building the extension bundle a service ships to igni.

    igni-service-bundle --extension extension/ --out extension-bundle.tar.gz

A gzipped tar with `manifest.json` at its root (declaring the `python`
packaging), the extension's own files — `README.md`, `plugin/`, `ui/`
when present — and a wheelhouse: the extension's wheel and every
dependency's, for CPython 3.12 on each platform igni runs on (macOS
arm64, Linux x86_64, Windows x86_64), so a machine with no network can
install it.

Per platform, the dependencies are resolved **for that platform** with
`uv pip compile --python-platform … --python-version 3.12` (markers such
as `sys_platform == 'win32'` are evaluated for the target, not for the
machine building), then each pinned requirement is downloaded as a wheel
with `pip download --only-binary=:all: --platform …`.

**A requirement with no usable wheel stops the build**, by name and
platform, unless the extension declares it sdist-only — in its
`pyproject.toml`:

    [tool.igni.bundle]
    sdist-only = ["wikipedia"]

or with `--allow-sdist wikipedia`. An allowed one is built from its sdist
and must come out pure Python (`py3-none-any`); anything else still stops
the build. Nothing is skipped silently.

The archive is deterministic — sorted members, zero mtimes and owners, a
zero gzip timestamp — so the same inputs give the same SHA-256, and the
service uploads only when the bundle really changed.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from collections.abc import Iterable, Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from igni_service.errors import BundleError

PYTHON_VERSION = "3.12"


class Platform(BaseModel):
    """One platform igni runs on, as uv and pip name it."""

    model_config = ConfigDict(frozen=True)

    name: str
    #: `uv pip compile --python-platform`.
    uv: str
    #: `pip download --platform`, each one; pip does not widen a
    #: `manylinux_2_x` tag to the older ones on its own.
    pip: tuple[str, ...]


PLATFORMS: tuple[Platform, ...] = (
    Platform(name="macOS arm64", uv="aarch64-apple-darwin", pip=("macosx_13_0_arm64",)),
    Platform(
        name="Linux x86_64",
        uv="x86_64-manylinux_2_28",
        pip=tuple(f"manylinux_2_{minor}_x86_64" for minor in range(28, 16, -1))
        + ("manylinux2014_x86_64", "manylinux2010_x86_64", "manylinux1_x86_64", "linux_x86_64"),
    ),
    Platform(name="Windows x86_64", uv="x86_64-pc-windows-msvc", pip=("win_amd64",)),
)


class PythonPackaging(BaseModel):
    """The manifest's `python` packaging."""

    package: str
    entry: str
    wheels: str = "wheels"


class BundleManifest(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    version: str
    python: PythonPackaging


class BundleReport(BaseModel):
    """What was built, and what was said about it."""

    out: Path
    extension_id: str
    version: str
    sha256: str
    size: int
    wheels: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class Requirement:
    """PEP 503 names, the one way requirements are compared here."""

    _SEPARATORS = re.compile(r"[-_.]+")

    @classmethod
    def normalise(cls, name: str) -> str:
        return cls._SEPARATORS.sub("-", name).lower().strip()

    @classmethod
    def name_of(cls, requirement: str) -> str:
        return cls.normalise(re.split(r"[=<>!~;\[ ]", requirement, maxsplit=1)[0])


class CommandRunner:
    """Runs uv and pip, with a fixed timestamp for anything they build."""

    #: Wheels built here carry this as every file's timestamp, so building
    #: the same source twice gives the same bytes.
    SOURCE_DATE_EPOCH = "315532800"

    def run(
        self, command: Sequence[str], *, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "SOURCE_DATE_EPOCH": self.SOURCE_DATE_EPOCH}
        return subprocess.run(
            list(command), cwd=cwd, env=env, capture_output=True, text=True, check=False
        )


class BundleArchive:
    """A deterministic gzipped tar of a directory, manifest at its root."""

    @staticmethod
    def write(staging: Path, out: Path) -> bytes:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
            for path in sorted(staging.rglob("*")):
                if path.is_dir():
                    continue
                info = tarfile.TarInfo(path.relative_to(staging).as_posix())
                info.size = path.stat().st_size
                info.mtime = 0
                info.mode = 0o644
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                with path.open("rb") as handle:
                    tar.addfile(info, handle)
        zipped = io.BytesIO()
        with gzip.GzipFile(filename="", mode="wb", fileobj=zipped, mtime=0) as gz:
            gz.write(buffer.getvalue())
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(zipped.getvalue())
        return zipped.getvalue()


class BundleBuilder:
    """Builds one extension directory into a bundle."""

    #: What goes into the bundle from the extension directory, besides the
    #: wheelhouse. Sources travel inside the extension's wheel; tests do not
    #: travel at all.
    SHIPPED = ("manifest.json", "README.md", "plugin", "ui")

    def __init__(
        self,
        extension_dir: Path,
        *,
        allow_sdist: Iterable[str] = (),
        find_links: Iterable[Path] = (),
        no_index: bool = False,
        build_isolation: bool = True,
        platforms: Sequence[Platform] = PLATFORMS,
        runner: CommandRunner | None = None,
    ) -> None:
        self._dir = extension_dir.resolve()
        self._find_links = [path.resolve() for path in find_links]
        self._no_index = no_index
        self._build_isolation = build_isolation
        self._platforms = tuple(platforms)
        self._runner = runner or CommandRunner()
        self._pyproject = self._read_pyproject()
        self._allow_sdist = {
            Requirement.normalise(name) for name in allow_sdist
        } | self._declared_sdist_only()

    # ── Reading the extension ───────────────────────────────────────

    def manifest(self) -> BundleManifest:
        path = self._dir / "manifest.json"
        try:
            return BundleManifest.model_validate(json.loads(path.read_text()))
        except FileNotFoundError:
            raise BundleError(f"{path} does not exist") from None
        except (ValueError, ValidationError) as exc:
            raise BundleError(
                f"{path} must name an id, a version and the `python` packaging: {exc}"
            ) from exc

    def _read_pyproject(self) -> dict[str, object]:
        path = self._dir / "pyproject.toml"
        try:
            return tomllib.loads(path.read_text())
        except FileNotFoundError:
            raise BundleError(f"{path} does not exist: the extension is a Python package") from None
        except tomllib.TOMLDecodeError as exc:
            raise BundleError(f"{path} is not valid TOML: {exc}") from exc

    def _table(self, *keys: str) -> dict[str, object]:
        table: object = self._pyproject
        for key in keys:
            table = table.get(key, {}) if isinstance(table, dict) else {}
        return table if isinstance(table, dict) else {}

    def _declared_sdist_only(self) -> set[str]:
        declared = self._table("tool", "igni", "bundle").get("sdist-only", [])
        if not isinstance(declared, list):
            raise BundleError("[tool.igni.bundle] sdist-only must be a list of names")
        return {Requirement.normalise(str(name)) for name in declared}

    def _check_package(self, manifest: BundleManifest) -> None:
        project = self._table("project")
        name = str(project.get("name", ""))
        if Requirement.normalise(name) != Requirement.normalise(manifest.python.package):
            raise BundleError(
                f"manifest.json installs {manifest.python.package!r} but pyproject.toml "
                f"builds {name!r}"
            )
        scripts = project.get("scripts", {})
        if not isinstance(scripts, dict) or manifest.python.entry not in scripts:
            raise BundleError(
                f"manifest.json starts {manifest.python.entry!r}, which pyproject.toml "
                "does not declare under [project.scripts]"
            )

    # ── Building ────────────────────────────────────────────────────

    def build(self, out: Path) -> BundleReport:
        manifest = self.manifest()
        self._check_package(manifest)
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp)
            staging = scratch / "bundle"
            staging.mkdir()
            for name in self.SHIPPED:
                self._add(staging, self._dir / name, name)
            wheels = staging / manifest.python.wheels
            wheels.mkdir(parents=True)
            self._extension_wheel(manifest, wheels, scratch)
            notes = self._wheelhouse(wheels, scratch)
            archive = BundleArchive.write(staging, out)
            built = sorted(path.name for path in wheels.glob("*.whl"))
        return BundleReport(
            out=out,
            extension_id=manifest.id,
            version=manifest.version,
            sha256=hashlib.sha256(archive).hexdigest(),
            size=len(archive),
            wheels=built,
            notes=notes,
        )

    @staticmethod
    def _add(staging: Path, source: Path, name: str) -> None:
        target = staging / name
        if source.is_dir():
            shutil.copytree(
                source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")
            )
        elif source.is_file():
            shutil.copy2(source, target)

    def _index_flags(self) -> list[str]:
        flags = ["--no-index"] if self._no_index else []
        for path in self._find_links:
            flags += ["--find-links", str(path)]
        return flags

    def _isolation_flags(self) -> list[str]:
        if self._build_isolation:
            return []
        return ["--no-build-isolation", "--python", sys.executable]

    def _extension_wheel(self, manifest: BundleManifest, into: Path, scratch: Path) -> Path:
        dist = scratch / "dist"
        built = self._runner.run(
            [
                "uv",
                "build",
                "--wheel",
                "--out-dir",
                str(dist),
                *self._index_flags(),
                *self._isolation_flags(),
                str(self._dir),
            ]
        )
        if built.returncode != 0:
            raise BundleError(f"could not build the extension's wheel:\n{built.stderr}")
        wheels = sorted(dist.glob("*.whl"))
        if len(wheels) != 1:
            raise BundleError(f"expected one extension wheel, found {[w.name for w in wheels]}")
        if Requirement.normalise(wheels[0].name.split("-")[0]) != Requirement.normalise(
            manifest.python.package
        ):
            raise BundleError(
                f"the extension built {wheels[0].name}, not {manifest.python.package}"
            )
        return Path(shutil.copy2(wheels[0], into / wheels[0].name))

    def _requirements(self, platform: Platform, scratch: Path) -> list[str]:
        out = scratch / f"requirements-{platform.uv}.txt"
        compiled = self._runner.run(
            [
                "uv",
                "pip",
                "compile",
                str(self._dir / "pyproject.toml"),
                "--python-version",
                PYTHON_VERSION,
                "--python-platform",
                platform.uv,
                "--no-header",
                "--no-annotate",
                "--quiet",
                # A bundle is resolved as a machine with no checkout will
                # install it: from wheels and indexes only. An enclosing
                # uv workspace's path sources would otherwise turn the
                # SDKs into editable paths that exist only here.
                "--no-sources",
                *self._index_flags(),
                *self._isolation_flags(),
                "-o",
                str(out),
            ]
        )
        if compiled.returncode != 0:
            raise BundleError(
                f"could not resolve the dependencies for {platform.name}:\n{compiled.stderr}"
            )
        return [
            line.strip()
            for line in out.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith(("#", "-"))
        ]

    def _pip(self) -> list[str]:
        """pip, from this interpreter if it has one, otherwise through uv."""
        if self._runner.run([sys.executable, "-m", "pip", "--version"]).returncode == 0:
            return [sys.executable, "-m", "pip"]
        return ["uvx", "--from", "pip", "pip"]

    def _download_command(self, pip: list[str], platform: Platform, into: Path) -> list[str]:
        return [
            *pip,
            "download",
            "--quiet",
            "--disable-pip-version-check",
            "--no-deps",
            "--only-binary=:all:",
            "--python-version",
            PYTHON_VERSION,
            "--implementation",
            "cp",
            "--abi",
            "cp312",
            "--abi",
            "abi3",
            "--abi",
            "none",
            *[flag for tag in platform.pip for flag in ("--platform", tag)],
            *self._index_flags(),
            "--dest",
            str(into),
        ]

    def _download(
        self, pip: list[str], requirements: list[str], platform: Platform, into: Path
    ) -> list[str]:
        """Download what has a wheel for `platform`; answer with what has none.

        One pip run for the lot. pip stops at the first requirement it
        cannot satisfy and names it, so that one is set aside and the rest
        asked for again until pip is satisfied.
        """
        remaining = list(requirements)
        missing: list[str] = []
        while remaining:
            listed = into.parent / f"want-{platform.uv}.txt"
            listed.write_text("\n".join(remaining) + "\n")
            done = self._runner.run(
                [*self._download_command(pip, platform, into), "-r", str(listed)]
            )
            listed.unlink()
            if done.returncode == 0:
                return missing
            named = re.search(r"No matching distribution found for (\S+)", done.stderr)
            wanted = Requirement.name_of(named.group(1)) if named else None
            culprit = next((r for r in remaining if Requirement.name_of(r) == wanted), None)
            if culprit is None:
                raise BundleError(f"pip download failed for {platform.name}:\n{done.stderr}")
            missing.append(culprit)
            remaining.remove(culprit)
        return missing

    def _wheel_from_sdist(
        self, pip: list[str], requirement: str, into: Path, scratch: Path
    ) -> Path:
        built = scratch / "sdist-wheels"
        built.mkdir(exist_ok=True)
        isolation = [] if self._build_isolation else ["--no-build-isolation"]
        made = self._runner.run(
            [
                *pip,
                "wheel",
                "--quiet",
                "--disable-pip-version-check",
                "--no-deps",
                "--no-binary",
                ":all:",
                *self._index_flags(),
                *isolation,
                "-w",
                str(built),
                requirement,
            ]
        )
        if made.returncode != 0:
            raise BundleError(
                f"{requirement}: could not build a wheel from its sdist:\n{made.stderr}"
            )
        name = Requirement.name_of(requirement)
        for wheel in sorted(built.glob("*.whl")):
            if Requirement.normalise(wheel.name.split("-")[0]) != name:
                continue
            if not wheel.name.endswith("-none-any.whl"):
                raise BundleError(
                    f"{requirement}: built {wheel.name} from its sdist, which is not pure Python "
                    "and so would install on one platform only"
                )
            return Path(shutil.copy2(wheel, into / wheel.name))
        raise BundleError(f"{requirement}: building its sdist produced no wheel")

    def _wheelhouse(self, into: Path, scratch: Path) -> list[str]:
        """Fill `into` with every wheel each platform needs. Returns what was said about it."""
        pip = self._pip()
        missing: dict[str, list[str]] = {}
        for platform in self._platforms:
            for requirement in self._download(
                pip, self._requirements(platform, scratch), platform, into
            ):
                missing.setdefault(requirement, []).append(platform.name)
        notes: list[str] = []
        unresolved: list[str] = []
        for requirement, platforms in sorted(missing.items()):
            if Requirement.name_of(requirement) in self._allow_sdist:
                built = self._wheel_from_sdist(pip, requirement, into, scratch)
                notes.append(
                    f"{requirement}: no wheel for {', '.join(platforms)}; built {built.name} "
                    "from its sdist (declared sdist-only)"
                )
                continue
            unresolved.append(f"{requirement}: no cp312/abi3/pure wheel for {', '.join(platforms)}")
        if unresolved:
            raise BundleError(
                "dependencies with no usable wheel:\n  "
                + "\n  ".join(unresolved)
                + "\nA pure-Python dependency that publishes only an sdist can be declared in "
                "pyproject.toml under [tool.igni.bundle] sdist-only = [...]."
            )
        return notes


class BundleCommand:
    """`igni-service-bundle`: the builder as a command."""

    @staticmethod
    def parser() -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(
            prog="igni-service-bundle",
            description="Build the extension bundle a service ships to igni.",
        )
        parser.add_argument(
            "--extension", type=Path, default=Path("extension"), help="the extension's directory"
        )
        parser.add_argument("--out", type=Path, default=Path("extension-bundle.tar.gz"))
        parser.add_argument(
            "--allow-sdist",
            action="append",
            default=[],
            metavar="NAME",
            help="a dependency that publishes no wheel and may be built from its sdist",
        )
        parser.add_argument("--find-links", action="append", type=Path, default=[], metavar="DIR")
        parser.add_argument("--no-index", action="store_true")
        parser.add_argument(
            "--no-build-isolation",
            action="store_true",
            help="build with this interpreter's packages instead of fetching build backends",
        )
        return parser

    @classmethod
    def main(cls, argv: Sequence[str] | None = None) -> int:
        args = cls.parser().parse_args(argv)
        try:
            report = BundleBuilder(
                args.extension,
                allow_sdist=args.allow_sdist,
                find_links=args.find_links,
                no_index=args.no_index,
                build_isolation=not args.no_build_isolation,
            ).build(args.out)
        except BundleError as exc:
            print(f"extension bundle: {exc}", file=sys.stderr)
            return 1
        for line in report.notes:
            print(f"extension bundle: {line}")
        print(
            f"extension bundle: wrote {report.out} — {report.extension_id} {report.version}, "
            f"{len(report.wheels)} wheels, {report.size} bytes, sha256 {report.sha256}"
        )
        return 0


def main(argv: Sequence[str] | None = None) -> int:
    """The console script's entry point."""
    return BundleCommand.main(argv)


if __name__ == "__main__":
    sys.exit(main())

"""A private Python environment for each ``python``-packaged extension.

The ``python`` packaging (igni server's ``docs/service-identity.md`` §8) is how a
service's extension runs on a person's machine: a package that speaks
MCP over stdio, shipped with its whole wheelhouse. This module turns
that into an executable.

**One environment per extension and version**, under the store, made
with igni's own interpreter — igni runs on CPython 3.12, and that is
what the service built its ``cp312`` wheels for. Private, because two
extensions pinning different versions of one library is the normal
case, and igni's own environment is not theirs to change.

**Offline, always, for a distributed bundle.** ``pip install
--no-index --find-links <wheelhouse> --only-binary :all:``, with pip's
own configuration files ignored: an air-gapped deployment has no index,
and a machine that has one must not quietly use it, or the bundle a
super admin approved would not be what runs. An sdist would need a
build backend from an index, so wheels only.

**A development install may use the network.** A source checkout has
no wheelhouse; the package is built into one from the directory —
``pip wheel``, with whatever index the developer's pip is configured
for — and then installed from it exactly as a distributed one is. One
install path, and the build step proves the directory really builds
the ``package`` the manifest names.

An environment is finished when its marker is written, last. One
without it was interrupted and is rebuilt rather than trusted.

**A development install is keyed by what it is made of, not its
version.** A developer reinstalls changed code under the same version
all day; keyed by version, the second install ran the first one's
environment. So a development environment records a digest of its
wheelhouse — or of the source directory it is built from — and is
rebuilt when that changes. A distributed bundle stays keyed by version:
its contents were verified against the service's signature, and a new
build is a new version.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import sys
from pathlib import Path

from pydantic import BaseModel

from ember_code.core.paths import CONFIG_DIR
from ember_code.extensions.manifest import ExtensionManifest

logger = logging.getLogger(__name__)

#: Written into an environment once it is complete.
READY_MARKER = ".igni-ready.json"

#: Where a development install's wheelhouse is built, inside its
#: environment so it goes when the environment does.
BUILT_WHEELHOUSE = ".wheelhouse"

#: How much of a failed command's output a refusal quotes.
OUTPUT_TAIL = 2000


class PythonEnvironmentError(Exception):
    """The environment could not be built, and why."""


class CommandOutcome(BaseModel):
    returncode: int
    output: str


class CommandRunner:
    """Runs one command to completion without blocking the loop.

    A class so a test can substitute one that records what would have
    run, for the steps that need a network.
    """

    async def run(
        self, argv: list[str], *, env: dict[str, str] | None = None, cwd: Path | None = None
    ) -> CommandOutcome:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
            cwd=str(cwd) if cwd is not None else None,
        )
        output, _ = await process.communicate()
        return CommandOutcome(
            returncode=process.returncode if process.returncode is not None else -1,
            output=output.decode("utf-8", errors="replace"),
        )


class EnvironmentRecord(BaseModel):
    """What the marker says was installed."""

    extension_id: str
    version: str
    package: str
    #: ``wheelhouse`` — the bundle's own, offline. ``source`` — built
    #: from a development directory first.
    installed_from: str
    #: For a development install, the digest of what it was built from;
    #: a different digest is a rebuild. Empty for a distributed bundle.
    content: str = ""


class ContentDigest:
    """What a development install is made of, as one hash.

    Every file's path and bytes, in a stable order, skipping what a
    checkout accumulates without its code changing — caches, virtual
    environments, version control.
    """

    IGNORED = frozenset(
        {
            ".git",
            ".hg",
            "__pycache__",
            ".venv",
            "venv",
            "node_modules",
            ".mypy_cache",
            ".pytest_cache",
            ".ruff_cache",
            "build",
            "dist",
            CONFIG_DIR,
        }
    )

    @classmethod
    def of(cls, root: Path) -> str:
        digest = hashlib.sha256()
        for path in sorted(cls._files(root)):
            relative = path.relative_to(root).as_posix()
            digest.update(relative.encode("utf-8") + b"\0")
            try:
                digest.update(path.read_bytes())
            except OSError:
                continue
            digest.update(b"\0")
        return digest.hexdigest()

    @classmethod
    def _files(cls, root: Path) -> list[Path]:
        found: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames if d not in cls.IGNORED and not d.endswith(".egg-info")
            ]
            found.extend(Path(dirpath) / name for name in filenames if not name.endswith(".pyc"))
        return found


class PythonEnvironments:
    """The private environments under one root."""

    def __init__(
        self,
        root: Path,
        *,
        interpreter: str | None = None,
        runner: CommandRunner | None = None,
    ) -> None:
        self._root = root
        self._interpreter = interpreter or sys.executable
        self._runner = runner or CommandRunner()

    @property
    def root(self) -> Path:
        return self._root

    def path_for(self, manifest: ExtensionManifest) -> Path:
        return self._root / manifest.id / manifest.version

    @staticmethod
    def _bin(environment: Path) -> Path:
        return environment / ("Scripts" if os.name == "nt" else "bin")

    @classmethod
    def executable(cls, environment: Path, name: str) -> Path:
        """A console script inside ``environment``."""
        suffix = ".exe" if os.name == "nt" else ""
        return cls._bin(environment) / f"{name}{suffix}"

    async def ensure(self, manifest: ExtensionManifest, bundle: Path, *, development: bool) -> Path:
        """The extension's ``entry``, installed and ready to start.

        Args:
            development: Whether this is a development install, which
                may build the package from the bundle directory when it
                carries no wheelhouse. A distributed bundle never may.

        Raises:
            PythonEnvironmentError: with what failed and pip's own words.
        """
        package = manifest.python
        if package is None:
            raise PythonEnvironmentError(f"{manifest.id} is not packaged as 'python'")

        environment = self.path_for(manifest)
        entry = self.executable(environment, package.entry)
        wheelhouse = self._wheelhouse(manifest, bundle)
        content = ContentDigest.of(wheelhouse or bundle) if development else ""
        if (environment / READY_MARKER).is_file() and entry.exists():
            if self._built_from(environment) == content:
                return entry
            logger.info(
                "rebuilding %s: its development install changed under version %s",
                manifest.id,
                manifest.version,
            )

        if wheelhouse is None and not development:
            raise PythonEnvironmentError(
                f"{manifest.id} has no wheelhouse at {package.wheels!r} in its bundle. "
                "A bundle igni distributes installs from its own wheels only — "
                "never an index — so it must carry them."
            )

        self._prune_other_versions(manifest)
        shutil.rmtree(environment, ignore_errors=True)
        environment.parent.mkdir(parents=True, exist_ok=True)

        await self._step(
            [self._interpreter, "-m", "venv", str(environment)],
            what=f"creating {manifest.id}'s environment",
        )
        python = self.executable(environment, "python")

        installed_from = "wheelhouse"
        if wheelhouse is None:
            wheelhouse = environment / BUILT_WHEELHOUSE
            installed_from = "source"
            logger.info("building %s from %s (development install)", manifest.id, bundle)
            await self._step(
                [
                    str(python),
                    "-m",
                    "pip",
                    "wheel",
                    "--disable-pip-version-check",
                    "--no-input",
                    "--wheel-dir",
                    str(wheelhouse),
                    str(bundle),
                ],
                what=f"building {manifest.id} from {bundle}",
            )

        await self._step(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--no-index",
                "--find-links",
                str(wheelhouse),
                "--only-binary",
                ":all:",
                "--disable-pip-version-check",
                "--no-input",
                package.package,
            ],
            what=f"installing {package.package} from its wheelhouse",
            env=self._offline_environment(),
        )

        if not entry.exists():
            raise PythonEnvironmentError(
                f"{package.package} installed and has no console script "
                f"{package.entry!r}, which is what the manifest says to run."
            )

        record = EnvironmentRecord(
            extension_id=manifest.id,
            version=manifest.version,
            package=package.package,
            installed_from=installed_from,
            content=content,
        )
        (environment / READY_MARKER).write_text(record.model_dump_json(indent=2), encoding="utf-8")
        logger.info("extension %s installed into %s", manifest.id, environment)
        return entry

    @staticmethod
    def _built_from(environment: Path) -> str | None:
        """The content digest the marker records; ``None`` if unreadable."""
        try:
            record = EnvironmentRecord.model_validate_json(
                (environment / READY_MARKER).read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return None
        return record.content

    def _wheelhouse(self, manifest: ExtensionManifest, bundle: Path) -> Path | None:
        """The bundle's wheelhouse, when it has one inside itself."""
        assert manifest.python is not None
        root = bundle.resolve()
        candidate = (bundle / manifest.python.wheels).resolve()
        if candidate != root and root not in candidate.parents:
            raise PythonEnvironmentError(
                f"{manifest.id}'s wheelhouse {manifest.python.wheels!r} is outside its bundle"
            )
        if candidate.is_dir() and any(candidate.glob("*.whl")):
            return candidate
        return None

    def _prune_other_versions(self, manifest: ExtensionManifest) -> None:
        """An environment for an older version is not coming back."""
        parent = self._root / manifest.id
        if not parent.is_dir():
            return
        for stale in parent.iterdir():
            if stale.name != manifest.version and stale.is_dir():
                shutil.rmtree(stale, ignore_errors=True)

    @staticmethod
    def _offline_environment() -> dict[str, str]:
        """pip's environment for an install that must not reach an index.

        ``--no-index`` already wins over an index URL; dropping every
        ``PIP_*`` variable and pointing pip at no configuration file
        means nothing else — an extra ``find-links``, a trusted host —
        is consulted either.
        """
        env = {key: value for key, value in os.environ.items() if not key.startswith("PIP_")}
        env["PIP_CONFIG_FILE"] = os.devnull
        env["PIP_NO_INPUT"] = "1"
        return env

    async def _step(self, argv: list[str], *, what: str, env: dict[str, str] | None = None) -> None:
        try:
            outcome = await self._runner.run(argv, env=env)
        except OSError as exc:
            raise PythonEnvironmentError(f"{what} failed: {exc}") from exc
        if outcome.returncode != 0:
            raise PythonEnvironmentError(f"{what} failed:\n{outcome.output[-OUTPUT_TAIL:]}")

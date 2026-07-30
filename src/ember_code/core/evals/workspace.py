"""RAII workspace lifecycle for eval suites.

Wraps the temp work-dir that holds fixture files + is the cwd the
agent's tools operate on. Everything the runner used to do inline —
tempdir allocation, fixture copy, optional ``setup_module`` import,
teardown — is now a single async context manager whose ``__aexit__``
guarantees cleanup even if a case raises.

The ``sys.path.insert`` needed to import a suite's ``setup_module``
is scoped to the workspace lifetime (inserted on ``__aenter__``,
popped on ``__aexit__``) rather than mutating global state
permanently the way the free-function ``_run_setup_module`` used to.
"""

from __future__ import annotations

import contextlib
import importlib
import inspect
import logging
import shutil
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import TracebackType

    from ember_code.core.evals.schemas import FixtureSpec

logger = logging.getLogger(__name__)


class EvalWorkspace:
    """Async context manager wrapping the per-suite work_dir lifecycle.

    Usage::

        async with EvalWorkspace(fixtures, fixtures_root, setup_module, project_dir) as work_dir:
            # run cases against work_dir …

    On enter: allocate tempdir, copy fixtures, (optionally) insert
    project_dir on sys.path and run the suite's ``setup_module.setup()``
    hook. On exit: pop the sys.path entry we added (if we added one)
    and ``rmtree`` the tempdir.
    """

    def __init__(
        self,
        fixtures: list[FixtureSpec] | None,
        fixtures_root: Path,
        setup_module: str | None,
        project_dir: Path,
    ) -> None:
        self._fixtures = fixtures
        self._fixtures_root = fixtures_root
        self._setup_module = setup_module
        self._project_dir = project_dir
        self._work_dir: Path | None = None
        self._added_sys_path_entry: str | None = None

    async def setup(self) -> Path:
        """Allocate the workspace and (optionally) run the setup hook.

        Public entry point so callers that need to distinguish
        setup failure from case-body failure can catch around this
        specifically without poking dunders. The context-manager
        surface (:meth:`__aenter__`) delegates here.
        """
        self._work_dir = self._allocate_and_copy_fixtures()
        if self._setup_module:
            await self._import_and_run_setup_module(self._work_dir)
        return self._work_dir

    async def teardown(self) -> None:
        """Pop the sys.path entry we added (if any) and rmtree the
        temp dir. Best-effort — a foreign entry could have displaced
        ours by now, in which case we leave it alone."""
        if self._added_sys_path_entry is not None:
            with contextlib.suppress(ValueError):
                sys.path.remove(self._added_sys_path_entry)
            self._added_sys_path_entry = None

        if self._work_dir is not None:
            try:
                shutil.rmtree(self._work_dir, ignore_errors=True)
            except Exception as cleanup_exc:
                # Defensive — ``rmtree(ignore_errors=True)`` already
                # swallows most errors; this catches the edge cases.
                logger.debug(
                    "Failed to clean up eval work dir %s: %s",
                    self._work_dir,
                    cleanup_exc,
                )
            self._work_dir = None

    async def __aenter__(self) -> Path:
        return await self.setup()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.teardown()

    def _allocate_and_copy_fixtures(self) -> Path:
        """Allocate a fresh temp dir and copy fixture files into it.

        ``target`` paths are interpreted relative to the temp dir.
        ``source`` is resolved relative to :attr:`_fixtures_root`
        (e.g. ``evals/fixtures/`` for committed datasets, or
        ``.ember/evals/`` for user-authored ones).
        """
        work_dir = Path(tempfile.mkdtemp(prefix="ember-eval-"))
        if not self._fixtures:
            return work_dir

        for fix in self._fixtures:
            src = self._fixtures_root / fix.source
            target_str = fix.target
            if not src.exists() or not target_str:
                logger.debug(
                    "fixture skip: src=%s exists=%s target=%r",
                    src,
                    src.exists(),
                    target_str,
                )
                continue
            target = work_dir / target_str
            target.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, target, dirs_exist_ok=True)
            else:
                shutil.copy2(src, target)
        return work_dir

    async def _import_and_run_setup_module(self, work_dir: Path) -> None:
        """Import ``self._setup_module`` and call its
        ``setup(work_dir, project_dir)`` hook.

        The setup function is the eval's own pre-run hook — it's where
        custom suites do work that fixture-copy can't express: git-initing
        the work_dir, applying a JSONL changeset, seeding a database.
        Two args go in: the per-suite work_dir (where the agent runs)
        and project_dir (the repo root, so the setup can find sibling
        fixture files like a snapshot).
        """
        # Make the project's evals/ importable so suites in
        # ``evals/<name>.yaml`` can reference ``evals.<name>.setup``.
        project_str = str(self._project_dir)
        if project_str not in sys.path:
            sys.path.insert(0, project_str)
            self._added_sys_path_entry = project_str

        module = importlib.import_module(self._setup_module)  # type: ignore[arg-type]
        setup_fn = getattr(module, "setup", None)
        if setup_fn is None:
            raise RuntimeError(f"setup_module {self._setup_module!r} has no ``setup`` callable")
        result = setup_fn(work_dir, self._project_dir)
        if inspect.isawaitable(result):
            await result

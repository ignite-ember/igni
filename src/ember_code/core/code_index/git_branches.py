"""Read local git branch heads for retention.

Wraps ``git for-each-ref refs/heads/`` behind a class so the subprocess
seam is isolated (testable), and returns a Pydantic model instead of
the raw ``dict[branch, sha]`` the free-function form used to return.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from ember_code.core.code_index.schema.branches import BranchHeadMap

logger = logging.getLogger(__name__)


class GitBranchReader:
    """Resolve local git branches to their head shas.

    Returns an empty :class:`BranchHeadMap` on any subprocess failure so
    the retention path stays branch-agnostic on non-git working trees.
    """

    TIMEOUT_SECONDS: float = 5.0

    def load(self, project: str | Path) -> BranchHeadMap:
        """Return ``{branch_name: head_sha}`` for every local branch.

        Empty when the project isn't a git repo (or when the git binary
        isn't on ``$PATH``, or the command times out).
        """
        try:
            result = subprocess.run(
                [
                    "git",
                    "for-each-ref",
                    "--format=%(refname:short) %(objectname)",
                    "refs/heads/",
                ],
                capture_output=True,
                text=True,
                cwd=str(project),
                timeout=self.TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("git for-each-ref failed: %s", exc)
            return BranchHeadMap(heads={})
        if result.returncode != 0:
            return BranchHeadMap(heads={})
        heads: dict[str, str] = {}
        for line in result.stdout.splitlines():
            parts = line.strip().split(maxsplit=1)
            if len(parts) == 2:
                branch, sha = parts
                heads[branch] = sha
        return BranchHeadMap(heads=heads)

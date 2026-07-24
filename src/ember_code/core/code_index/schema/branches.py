"""Typed wrapper around ``git for-each-ref refs/heads/`` output.

Replaces the raw ``dict[branch_name, head_sha]`` return of the old
``_branch_heads`` free function, so the retention path in
:meth:`CodeIndex.clean` can rely on a Pydantic surface rather than
guessing at the map orientation.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class BranchHeadMap(BaseModel):
    """Map of ``{branch_name: head_sha}`` with an inverted-view helper."""

    heads: dict[str, str] = Field(default_factory=dict)

    def per_commit(self) -> dict[str, list[str]]:
        """Invert to ``{head_sha: [branch_name, ...]}``.

        Empty when :attr:`heads` is empty. Multiple branches pointing at
        the same commit land in one list.
        """
        out: dict[str, list[str]] = {}
        for branch, sha in self.heads.items():
            out.setdefault(sha, []).append(branch)
        return out

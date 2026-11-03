"""``.igni/.gitignore`` — the personal and runtime files, and nothing else.

``.igni/`` is half shared configuration and half one person's state. The
shared half — ``agents/``, ``skills/``, ``hooks/``, ``workflows/``,
``output-styles/``, ``tools/``, ``evals/``, ``config.yaml``,
``settings.json``, ``rules.md``, ``.mcp.json``, ``plugins.json`` — is what a
team commits, so ignoring the directory wholesale is wrong. The other half
(``*.local`` overrides, ephemeral agents, databases, credentials, trust
decisions, logs, attachments, igni's own bookkeeping, the group policy pulled
from the server) is listed here and kept out.

igni owns one marked block in the file and rewrites only that block, so lines
a team adds above or below it survive every run. Most of the runtime entries
are written under ``~/.igni`` rather than the project's ``.igni``; they are
listed anyway, because a home directory can be a repository too (dotfiles)
and a project opened at ``~`` *is* ``~/.igni``.
"""

from __future__ import annotations

from pathlib import Path

BLOCK_BEGIN = "# >>> igni: personal and runtime files (igni rewrites this block) >>>"
BLOCK_END = "# <<< igni <<<"

#: The block's body, one gitignore line each. Comments say why a line is here.
BLOCK_LINES: tuple[str, ...] = (
    "# Shared, and committed: agents/ skills/ hooks/ workflows/ output-styles/",
    "# tools/ evals/ config.yaml settings.json rules.md .mcp.json plugins.json.",
    "# Edit outside this block; igni puts this block back as it was.",
    "#",
    "# One person's overrides: agents.local/, skills.local/, settings.local.json,",
    "# config.local.yaml, rules.local.md",
    "*.local",
    "*.local.*",
    "# Ephemeral agents (/agents create); /agents promote keeps one",
    "agents.tmp/",
    "# Sessions, memory, state",
    "*.db",
    "*.db-journal",
    "*.db-wal",
    "*.db-shm",
    "# Credentials and this machine's decisions",
    "credentials.json",
    "workspace-trust.json",
    "mcp-approved.json",
    "mcp-tool-state.json",
    "# Logs and what a run leaves behind",
    "*.log",
    "logs/",
    "process_logs/",
    "attachments/",
    "worktrees/",
    "projects/",
    "# igni's bookkeeping, and the update it could not apply over your edit",
    ".initialized",
    ".checksums.json",
    ".update-check",
    "*.new",
    "# The organisation's group policy, pulled from the server",
    "group-policy/",
)


class IgniGitignore:
    """Keeps igni's block in ``<config_dir>/.gitignore`` current."""

    def __init__(self, config_dir: Path) -> None:
        self.path = Path(config_dir) / ".gitignore"

    @staticmethod
    def block() -> str:
        return "\n".join((BLOCK_BEGIN, *BLOCK_LINES, BLOCK_END)) + "\n"

    def render(self, existing: str) -> str:
        """``existing`` with igni's block current and everything else as it was."""
        lines = existing.splitlines(keepends=True)
        begin = next((i for i, line in enumerate(lines) if line.rstrip("\n") == BLOCK_BEGIN), None)
        if begin is None:
            prefix = existing
            if prefix and not prefix.endswith("\n"):
                prefix += "\n"
            if prefix:
                prefix += "\n"
            return prefix + self.block()
        end = next(
            (i for i in range(begin + 1, len(lines)) if lines[i].rstrip("\n") == BLOCK_END),
            None,
        )
        if end is None:
            # The end marker was lost. Take back only the lines that are
            # igni's own, so nothing a person wrote below is dropped.
            ours = set(BLOCK_LINES)
            end = begin
            while end + 1 < len(lines) and lines[end + 1].rstrip("\n") in ours:
                end += 1
        before = "".join(lines[:begin])
        after = "".join(lines[end + 1 :])
        return before + self.block() + after

    def ensure(self) -> bool:
        """Write the block if it is missing or out of date. ``True`` if written."""
        try:
            existing = self.path.read_text(encoding="utf-8") if self.path.exists() else ""
        except OSError:
            return False
        wanted = self.render(existing)
        if wanted == existing:
            return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(wanted, encoding="utf-8")
        except OSError:
            return False
        return True


__all__ = ["BLOCK_BEGIN", "BLOCK_END", "BLOCK_LINES", "IgniGitignore"]

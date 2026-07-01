"""Markdown-authored slash commands — Claude Code parity.

A user can drop ``.md`` files into ``<project>/.claude/commands/``,
``<project>/.ember/commands/``, ``~/.claude/commands/``, or
``~/.ember/commands/`` to define new slash commands without writing
Python. The file's basename becomes the command name (so
``commands/review.md`` invokes as ``/review``); the body is the
template that gets sent to the agent after substitution.

Supported template tokens, evaluated in order:

* ``$ARGUMENTS`` → the rest of the user's input after the command
  name. Plain text substitution, no quoting.
* ``!`cmd``` → run ``cmd`` via the shell, capture stdout, inline
  it. Errors / non-zero exits are inlined as an ``[error: ...]``
  marker so the agent sees the failure rather than silently
  getting empty output.
* ``@<path>`` → inline the contents of the referenced file. Same
  shape as the rules-file ``@<path>.md`` syntax but extension-
  agnostic (commands frequently inline ``@README.md`` or
  ``@pyproject.toml``). Paths resolve against the command file's
  directory, with ``~`` expansion and project-relative
  interpretation.

YAML frontmatter (all optional) is parsed and stashed on the
returned ``MarkdownCommand``:

* ``description`` — one-line summary shown in help listings.
* ``allowed-tools`` — comma-separated list (CC parity). Currently
  parsed and exposed; gating is the agent's responsibility.
* ``argument-hint`` — completion hint like ``[path]`` or ``<name>``.
* ``model`` — preferred model identifier. Currently parsed and
  exposed; the session's ``/model`` controls the active model.

Precedence (lower → higher, last write wins on name collisions):

1. ``~/.claude/commands/`` (cross-tool user)
2. ``~/.ember/commands/`` (ember user)
3. ``<project>/.claude/commands/`` (cross-tool project)
4. ``<project>/.ember/commands/`` (ember project)

The ``read_claude`` toggle (settings.rules.cross_tool_support)
mirrors the rest of the cross-tool plumbing — flip it off and the
``.claude/commands/`` sources are skipped.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


# Frontmatter parser is intentionally permissive — accept anything
# YAML accepts, store whatever's there, ignore unknown keys. CC's
# command files are user-authored so brittle parsing would punish
# benign experimentation.
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)", re.DOTALL)

# Shell-injection token: ``!`...```. Greedy on the inner content but
# stops at the first closing backtick so adjacent tokens don't
# bleed. Multi-line shell commands need not apply.
_SHELL_TOKEN_RE = re.compile(r"!`([^`\n]+)`")

# ``@<path>`` token. Path runs to the first whitespace or closing
# paren so a markdown link like ``[label](@./foo)`` doesn't get
# truncated mid-URL. ``.md`` is intentionally NOT required — command
# templates routinely pull in ``@pyproject.toml`` / ``@Cargo.toml``.
_AT_FILE_RE = re.compile(r"@([^\s)]+)")

# How long any single shell substitution can run before we bail and
# inline an error marker. 30 seconds keeps a hung command from
# locking up the entire turn while still leaving room for slow git
# operations on large repos.
_SHELL_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class MarkdownCommand:
    """A single markdown-authored command discovered on disk."""

    name: str
    path: Path
    description: str = ""
    allowed_tools: tuple[str, ...] = field(default_factory=tuple)
    argument_hint: str = ""
    model: str | None = None
    body: str = ""

    async def render(self, args: str, *, project_dir: Path | None = None) -> str:
        """Resolve ``$ARGUMENTS``, ``!`cmd```, and ``@path`` tokens
        in the body and return the final prompt text."""
        text = self.body.replace("$ARGUMENTS", args)
        text = await _substitute_shell(text, cwd=project_dir or self.path.parent)
        text = _substitute_files(text, source=self.path, project_dir=project_dir)
        return text


# ── Discovery ────────────────────────────────────────────────────


def _commands_dirs(project_dir: Path, read_claude: bool) -> list[Path]:
    """Roots to scan, in load order (later overrides earlier)."""
    home = Path.home()
    roots: list[Path] = []
    if read_claude:
        roots.append(home / ".claude" / "commands")
    roots.append(home / ".ember" / "commands")
    if read_claude:
        roots.append(project_dir / ".claude" / "commands")
    roots.append(project_dir / ".ember" / "commands")
    return roots


def _parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Pull a YAML frontmatter block off the top of ``content``.

    Returns ``({}, content)`` when there's no frontmatter or the
    block can't be parsed — fail-open so a malformed header
    doesn't sink the whole command."""
    match = _FRONTMATTER_RE.match(content)
    if not match:
        return {}, content
    raw, body = match.group(1), match.group(2)
    try:
        meta = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        logger.debug("Markdown command frontmatter parse failed: %s", exc)
        return {}, body
    if not isinstance(meta, dict):
        return {}, body
    return meta, body


def _load_command_file(path: Path) -> MarkdownCommand | None:
    """Read one ``.md`` file and return a ``MarkdownCommand``, or
    ``None`` if the file is unreadable. The command name is the
    stem (so ``review.md`` → ``review``); leading ``.`` files (e.g.
    editor backups) are skipped."""
    if path.name.startswith("."):
        return None
    try:
        content = path.read_text()
    except (OSError, UnicodeDecodeError) as exc:
        logger.debug("Markdown command read %s failed: %s", path, exc)
        return None
    meta, body = _parse_frontmatter(content)
    allowed_raw = meta.get("allowed-tools", "")
    if isinstance(allowed_raw, str):
        allowed = tuple(t.strip() for t in allowed_raw.split(",") if t.strip())
    elif isinstance(allowed_raw, list):
        allowed = tuple(str(t).strip() for t in allowed_raw if str(t).strip())
    else:
        allowed = ()
    return MarkdownCommand(
        name=path.stem,
        path=path.resolve(),
        description=str(meta.get("description", "")).strip(),
        allowed_tools=allowed,
        argument_hint=str(meta.get("argument-hint", "")).strip(),
        model=str(meta["model"]).strip() if meta.get("model") else None,
        body=body,
    )


def discover_markdown_commands(
    project_dir: Path,
    read_claude: bool = True,
) -> dict[str, MarkdownCommand]:
    """Walk all configured roots and return ``name → command``.

    Later roots override earlier ones on name collisions — project
    commands beat user-global, ember beats claude (within the same
    tier). The leading-slash isn't part of the key (users invoke
    ``/review`` but the dict is keyed ``review``)."""
    out: dict[str, MarkdownCommand] = {}
    for root in _commands_dirs(project_dir, read_claude=read_claude):
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.md")):
            cmd = _load_command_file(path)
            if cmd is not None:
                out[cmd.name] = cmd
    return out


# ── Token substitution ──────────────────────────────────────────


async def _substitute_shell(text: str, *, cwd: Path) -> str:
    """Replace every ``!`cmd``` token with the captured stdout.

    Runs each command via ``/bin/sh -c`` (matching CC's shell-
    inline semantics) with a 30-second timeout. Non-zero exits and
    timeouts produce a clearly-marked inline error so the agent
    notices the failure instead of silently working from empty
    output.
    """
    # Collect tokens first so we can fan out concurrently — a
    # template that runs ``git status`` + ``git log`` doesn't need
    # to serialize them.
    matches = list(_SHELL_TOKEN_RE.finditer(text))
    if not matches:
        return text
    results = await asyncio.gather(
        *(_run_shell(m.group(1), cwd=cwd) for m in matches),
        return_exceptions=False,
    )

    def replace(m: re.Match[str]) -> str:
        idx = next(i for i, mm in enumerate(matches) if mm.start() == m.start())
        return results[idx]

    return _SHELL_TOKEN_RE.sub(replace, text)


async def _run_shell(cmd: str, *, cwd: Path) -> str:
    """Run a single shell snippet, capturing stdout. Errors come
    back as ``[error: ...]`` strings rather than raising so the
    caller can keep going."""
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return f"[error: {exc}]"
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=_SHELL_TIMEOUT_SECONDS)
    except TimeoutError:
        proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        return f"[error: timed out after {_SHELL_TIMEOUT_SECONDS}s — {shlex.quote(cmd)}]"
    if proc.returncode != 0:
        stderr_txt = err.decode("utf-8", errors="replace").strip()
        return f"[error: exit {proc.returncode} — {stderr_txt or shlex.quote(cmd)}]"
    return out.decode("utf-8", errors="replace").rstrip("\n")


def _substitute_files(text: str, *, source: Path, project_dir: Path | None) -> str:
    """Inline ``@path`` references. Paths resolve in order:
    user-home expansion, absolute, then relative to the command
    file's directory. Unreadable / non-existent paths are left as
    literals so the agent can see the broken reference."""

    def replace(m: re.Match[str]) -> str:
        token = m.group(1)
        # Trailing punctuation (``,`` ``.`` ``;`` etc.) is almost
        # always sentence flow, not part of the path. Strip it
        # back to the file separator so ``See @README.md.`` works.
        path_str = token.rstrip(",.;:!?)\"'")
        trailing = token[len(path_str) :]
        candidate: Path
        if path_str.startswith("~"):
            candidate = Path(path_str).expanduser()
        elif path_str.startswith("/"):
            candidate = Path(path_str)
        else:
            candidate = source.parent / path_str
        try:
            resolved = candidate.resolve()
            if not resolved.is_file():
                return m.group(0)
            # Defense in depth: when a project_dir is known, refuse
            # paths that escape it (an ``@/etc/passwd`` should not
            # silently exfiltrate). User-home command files have
            # no project_dir scoping — they're explicitly authored
            # by the user, so they can reference anywhere.
            if project_dir is not None:
                try:
                    resolved.relative_to(project_dir.resolve())
                except ValueError:
                    # Out-of-project reference from a PROJECT
                    # command — block. From a user command, the
                    # ``source.parent`` is in ``~/.ember/commands``
                    # which won't be under project_dir either, so
                    # we only enforce when source IS under project.
                    try:
                        source.resolve().relative_to(project_dir.resolve())
                        return m.group(0)
                    except ValueError:
                        pass
            return resolved.read_text() + trailing
        except (OSError, UnicodeDecodeError):
            return m.group(0)

    return _AT_FILE_RE.sub(replace, text)

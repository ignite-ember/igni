"""Skill parser — parses SKILL.md files with YAML frontmatter."""

import re
from pathlib import Path

import yaml
from pydantic import BaseModel

# Ordered alternation: the indexed form must be tried before the bare
# one, and ``\d+`` must be greedy. See :func:`_substitute_arguments`.
_ARG_TOKEN_RE = re.compile(r"\$ARGUMENTS\[(\d+)\]|\$ARGUMENTS|\$(\d+)")


def _substitute_arguments(body: str, arguments: str) -> str:
    """Replace ``$ARGUMENTS``, ``$ARGUMENTS[i]`` and ``$N`` in one pass.

    One pass rather than a sequence of :meth:`str.replace` rounds,
    because the sequential form made two of the documented tokens
    unreachable. A global ``$ARGUMENTS`` replace running first ate the
    prefix of ``$ARGUMENTS[0]`` and left ``alpha beta[0]`` in the body;
    replacing ``$1``..``$N`` in ascending order rewrote the ``$1`` of
    ``$10`` as soon as a skill took ten arguments. The alternation
    settles both: indexed before bare, longest run of digits wins.

    Substituted text is never rescanned, so an argument that itself
    contains ``$2`` stays literal instead of pulling in another arg.
    Out-of-range indices are left as written — a skill invoked with too
    few arguments shows the unfilled token rather than silently
    dropping it.
    """
    args = arguments.split()

    def replace(match: re.Match[str]) -> str:
        indexed, positional = match.group(1), match.group(2)
        if indexed is not None:  # $ARGUMENTS[i] — zero-based
            i = int(indexed)
        elif positional is not None:  # $N — one-based, so $0 never matches
            i = int(positional) - 1
        else:  # bare $ARGUMENTS — the whole string, spacing preserved
            return arguments
        return args[i] if 0 <= i < len(args) else match.group(0)

    return _ARG_TOKEN_RE.sub(replace, body)


class SkillDefinition(BaseModel):
    """Parsed skill definition from a SKILL.md file."""

    name: str
    description: str = ""
    version: str = "0.1.0"
    category: str = "development"
    argument_hint: str = ""
    context: str = "inline"
    agent: str | None = None
    user_invocable: bool = True
    body: str = ""
    source_dir: Path | None = None

    def render(self, arguments: str = "", session_id: str = "") -> str:
        """Render the skill body with argument substitutions."""
        text = _substitute_arguments(self.body, arguments)

        if self.source_dir:
            text = text.replace("${IGNI_SKILL_DIR}", str(self.source_dir))
            text = text.replace("${CLAUDE_SKILL_DIR}", str(self.source_dir))

        text = text.replace("${IGNI_SESSION_ID}", session_id)
        return text


class SkillInfo(BaseModel):
    """Wire format for one skill — emitted by
    :meth:`BackendServer.get_skill_details`, consumed by the skills
    panel.

    JSON-friendly subset of :class:`SkillDefinition`: ``source_dir``
    widened to ``str`` (Path doesn't round-trip), ``body`` is sent
    in full so the panel can head-clip for preview without an extra
    RPC. No ``render`` method — wire models are read-only.
    """

    name: str
    description: str = ""
    version: str = "0.1.0"
    category: str = "development"
    argument_hint: str = ""
    context: str = "inline"
    agent: str = ""
    user_invocable: bool = True
    body: str = ""
    source_dir: str = ""


def _as_str(value: object) -> str:
    """Coerce a value to string (YAML parses ``[hint]`` as a list)."""
    if isinstance(value, list):
        return " ".join(str(v) for v in value)
    return str(value) if value else ""


class SkillParser:
    """Parses skill definitions from SKILL.md files."""

    @staticmethod
    def parse(path: Path) -> SkillDefinition:
        """Parse a SKILL.md file into a SkillDefinition.

        The skill name defaults to the parent directory name
        (e.g. ``deploy/SKILL.md`` → ``deploy``).
        """
        content = path.read_text()
        default_name = path.parent.name

        fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)", content, re.DOTALL)
        if not fm_match:
            return SkillDefinition(
                name=default_name,
                body=content.strip(),
                source_dir=path.parent,
            )

        yaml_str = fm_match.group(1)
        body = fm_match.group(2).strip()
        fm = yaml.safe_load(yaml_str) or {}

        return SkillDefinition(
            name=fm.get("name", default_name),
            description=fm.get("description", ""),
            version=fm.get("version", "0.1.0"),
            category=fm.get("category", "development"),
            argument_hint=_as_str(fm.get("argument-hint", fm.get("argument_hint", ""))),
            context=fm.get("context", "inline"),
            agent=fm.get("agent"),
            user_invocable=fm.get("user-invocable", True),
            body=body,
            source_dir=path.parent,
        )

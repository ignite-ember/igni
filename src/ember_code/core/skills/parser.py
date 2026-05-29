"""Skill parser — parses SKILL.md files with YAML frontmatter."""

import re
from pathlib import Path

import yaml
from pydantic import BaseModel


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
        text = self.body
        text = text.replace("$ARGUMENTS", arguments)

        args = arguments.split() if arguments else []
        for i, arg in enumerate(args):
            text = text.replace(f"${i + 1}", arg)
            text = text.replace(f"$ARGUMENTS[{i}]", arg)

        if self.source_dir:
            text = text.replace("${EMBER_SKILL_DIR}", str(self.source_dir))
            text = text.replace("${CLAUDE_SKILL_DIR}", str(self.source_dir))

        text = text.replace("${EMBER_SESSION_ID}", session_id)
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

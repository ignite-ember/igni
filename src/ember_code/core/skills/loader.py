"""Skill loader — discovers and loads skills from directories."""

import sys
from pathlib import Path

from pydantic import BaseModel

from ember_code.core.skills.parser import SkillDefinition, SkillParser


class SkillEntry(BaseModel):
    """A skill in the pool with its priority."""

    definition: SkillDefinition
    priority: int


class SkillPool:
    """Manages the pool of available skills."""

    def __init__(self):
        self._entries: dict[str, SkillEntry] = {}
        self._parser = SkillParser()

    def load_directory(
        self,
        path: Path,
        priority: int = 0,
        namespace: str | None = None,
    ):
        """Load all skills from a directory.

        Each skill lives in a named subdirectory containing a SKILL.md file,
        e.g. ``deploy/SKILL.md``. Supporting files (templates, references)
        can be placed alongside SKILL.md in the same directory.
        Higher priority wins on name conflicts.

        ``namespace`` prefixes every loaded skill's ``name`` as
        ``<namespace>:<name>``. Used by the plugin loader so each
        plugin's skills land under their own namespace and can't
        collide with same-named skills from other plugins or from the
        user's own ``.ember/skills/``.
        """
        if not path.exists():
            return

        for skill_dir in sorted(path.iterdir()):
            if not skill_dir.is_dir():
                continue
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.exists():
                continue

            try:
                definition = self._parser.parse(skill_file)
                if namespace:
                    definition = definition.model_copy(
                        update={"name": f"{namespace}:{definition.name}"}
                    )
                name = definition.name

                if name not in self._entries or priority > self._entries[name].priority:
                    self._entries[name] = SkillEntry(
                        definition=definition,
                        priority=priority,
                    )
            except Exception as e:
                print(f"Warning: Failed to load skill from {skill_file}: {e}", file=sys.stderr)

    def load_all(self, project_dir: Path | None = None, cross_tool_support: bool = False):
        """Load skills from all directories in priority order."""
        if project_dir is None:
            project_dir = Path.cwd()

        # Priority 0: Built-in skills
        builtin_dir = Path(__file__).parent.parent / "bundled_skills"
        self.load_directory(builtin_dir, priority=0)

        # Priority 1: Global user skills
        global_dir = Path.home() / ".ember" / "skills"
        self.load_directory(global_dir, priority=1)

        # Priority 2: Project local skills (gitignored)
        local_dir = project_dir / ".ember" / "skills.local"
        self.load_directory(local_dir, priority=2)

        # Priority 3: Project skills
        project_skills = project_dir / ".ember" / "skills"
        self.load_directory(project_skills, priority=3)

        # Cross-tool support: Claude Code directories
        if cross_tool_support:
            claude_project = project_dir / ".claude" / "skills"
            self.load_directory(claude_project, priority=1)
            claude_global = Path.home() / ".claude" / "skills"
            self.load_directory(claude_global, priority=0)

    def get(self, name: str) -> SkillDefinition | None:
        """Get a skill by name."""
        entry = self._entries.get(name)
        return entry.definition if entry else None

    def list_skills(self) -> list[SkillDefinition]:
        """List all skill definitions."""
        return [entry.definition for entry in self._entries.values()]

    def list_by_category(self) -> dict[str, list[SkillDefinition]]:
        """Group skills by category."""
        categories: dict[str, list[SkillDefinition]] = {}
        for entry in self._entries.values():
            cat = entry.definition.category
            categories.setdefault(cat, []).append(entry.definition)
        return categories

    def describe(self) -> str:
        """Generate a summary of all skills for the Orchestrator, grouped by category."""
        by_cat = self.list_by_category()
        if not by_cat:
            return ""

        # Sort categories in a predictable order
        order = ["development", "review", "planning", "operations"]
        sorted_cats = sorted(by_cat.keys(), key=lambda c: (order.index(c) if c in order else 99, c))

        lines = []
        for cat in sorted_cats:
            lines.append(f"\n### {cat.title()}")
            for skill in sorted(by_cat[cat], key=lambda s: s.name):
                hint = f" {skill.argument_hint}" if skill.argument_hint else ""
                lines.append(f"- **/{skill.name}**{hint}: {skill.description}")
        return "\n".join(lines)

    def match_user_command(self, text: str) -> tuple[SkillDefinition, str] | None:
        """Check if user input matches a /skill-name command."""
        text = text.strip()
        if not text.startswith("/"):
            return None

        parts = text[1:].split(None, 1)
        name = parts[0]
        args = parts[1] if len(parts) > 1 else ""

        skill = self.get(name)
        if skill and skill.user_invocable:
            return (skill, args)
        return None

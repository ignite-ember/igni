"""Skill loader — discovers and loads skills from directories.

Resolution order (highest priority wins on name collision; integers are
explicit so the outcome doesn't depend on load order). Within the same
scope, native Ember sources beat cross-tool Claude sources by +1:

    5  <project>/.ember/skills/          (project, native)
    4  <project>/.ember/skills.local/    (project personal, gitignored)
    3  <project>/.claude/skills/         (project, cross-tool)
    2  ~/.ember/skills/                  (user, native)
    1  ~/.claude/skills/                 (user, cross-tool)
    0  core/bundled_skills/              (built-in defaults)

Plugins land under their own namespace (``<plugin>:<skill>``) so they
never collide with the base hierarchy. Two plugins of the same name
across roots are resolved one step earlier, inside ``PluginLoader``.
"""

import sys
from pathlib import Path

from pydantic import BaseModel

from ember_code.core.skills.parser import SkillDefinition, SkillParser


class SkillPriority:
    """Resolution priorities — see module docstring for the full table.

    Constants are integers so they compose with the existing
    ``load_directory(priority=...)`` API; callers (tests, plugins) can
    still pass any integer they want.
    """

    BUNDLED = 0
    USER_CLAUDE = 1
    USER_EMBER = 2
    PROJECT_CLAUDE = 3
    PROJECT_LOCAL = 4
    PROJECT_EMBER = 5


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
        """Load skills from all directories. See module docstring for the
        full resolution table — each source has an explicit integer
        priority so ties never depend on call order here.
        """
        if project_dir is None:
            project_dir = Path.cwd()

        # Built-in defaults (lowest).
        builtin_dir = Path(__file__).parent.parent / "bundled_skills"
        self.load_directory(builtin_dir, priority=SkillPriority.BUNDLED)

        # User-level Ember (beats user-level Claude by +1).
        self.load_directory(
            Path.home() / ".ember" / "skills", priority=SkillPriority.USER_EMBER
        )

        # Project-level personal overrides (gitignored).
        self.load_directory(
            project_dir / ".ember" / "skills.local", priority=SkillPriority.PROJECT_LOCAL
        )

        # Project-level Ember (highest).
        self.load_directory(
            project_dir / ".ember" / "skills", priority=SkillPriority.PROJECT_EMBER
        )

        # Cross-tool Claude Code directories — explicitly slotted *below*
        # their same-scope Ember equivalents.
        if cross_tool_support:
            self.load_directory(
                project_dir / ".claude" / "skills",
                priority=SkillPriority.PROJECT_CLAUDE,
            )
            self.load_directory(
                Path.home() / ".claude" / "skills",
                priority=SkillPriority.USER_CLAUDE,
            )

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

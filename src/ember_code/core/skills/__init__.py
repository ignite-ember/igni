"""Skills system — reusable prompted workflows via /skill-name."""

from ember_code.core.skills.executor import SkillExecutor, SkillResult
from ember_code.core.skills.loader import SkillEntry, SkillPool
from ember_code.core.skills.parser import SkillDefinition, SkillParser

__all__ = [
    "SkillPool",
    "SkillEntry",
    "SkillParser",
    "SkillDefinition",
    "SkillExecutor",
    "SkillResult",
]

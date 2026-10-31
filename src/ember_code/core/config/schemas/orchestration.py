"""Runtime-orchestration cluster of config schemas.

Groups the small structs that describe how the runtime orchestrates
agents, skills, rules, hooks, scheduler, and context. Each schema is
a handful of fields with no behavior; keeping them together prevents
the schemas package from bloating.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ember_code.core.paths import PROJECT_CONTEXT_FILE

#: What igni does with a hook your organisation's group policy ships.
#: Ordered from most to least permissive; :data:`GROUP_HOOK_MODES` is the
#: order the strictest-wins rule reads.
GroupHookMode = Literal["run", "ask", "refuse"]
GROUP_HOOK_MODES: tuple[GroupHookMode, ...] = ("run", "ask", "refuse")


class OrchestrationConfig(BaseModel):
    max_nesting_depth: int = 5
    max_total_agents: int = 20
    # Per-specialist deadline. 10 minutes was too aggressive for
    # reasoning-heavy broadcasts (security audits, large refactors)
    # where each specialist can chew through many tool calls. Bump
    # to 30m — long enough for a thorough analysis, short enough
    # that a hung model provider still gets killed before the
    # session feels frozen.
    sub_team_timeout: int = 1800
    max_task_iterations: int = 10
    generate_ephemeral: bool = True
    max_ephemeral_per_session: int = 5
    auto_cleanup: bool = True


class AgentsConfig(BaseModel):
    cross_tool_support: bool = True


class SkillsConfig(BaseModel):
    cross_tool_support: bool = True
    auto_trigger: bool = True
    default_agent: str = "editor"


class RulesConfig(BaseModel):
    cross_tool_support: bool = True


class HooksConfig(BaseModel):
    cross_tool_support: bool = True
    #: What igni does with the hooks the organisation's group policy ships
    #: (``group-policy/hooks`` and the hooks of a group-installed plugin):
    #:
    #: * ``run`` — run them, as before (the default);
    #: * ``ask`` — show the hook's command and where it came from, and run it
    #:   only once you say yes; asked once per version of the hook, and with
    #:   nobody to ask (a headless run) an unanswered hook is refused;
    #: * ``refuse`` — never run them.
    #:
    #: Yours alone: only ``~/.igni/config.yaml`` and the command line
    #: (``--strict`` means at least ``ask``) set it. A project's config, the
    #: group policy and the managed file cannot — the person this protects
    #: you from is whoever administers the organisation
    #: (``DeveloperOnlyTier``).
    group: GroupHookMode = "run"


class SchedulerConfig(BaseModel):
    poll_interval: int = 30
    task_timeout: int = 300
    max_concurrent: int = 1


class ContextConfig(BaseModel):
    project_file: str = PROJECT_CONTEXT_FILE
    ignore_patterns: list[str] = Field(
        default_factory=lambda: [
            "node_modules/",
            ".git/",
            "__pycache__/",
            "*.pyc",
            ".venv/",
            "dist/",
            "build/",
        ]
    )

"""Skill executor — runs skills inline or forked into a sub-agent."""

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from ember_code.core.utils.response import extract_response_text

if TYPE_CHECKING:
    from ember_code.core.agents import AgentPool
    from ember_code.core.config.settings import Settings
    from ember_code.core.skills.parser import SkillDefinition


class SkillResult(BaseModel):
    """Typed outcome of :meth:`SkillExecutor.execute`.

    Co-located in :mod:`executor` to match the package's inline-model
    convention (``parser.py``, ``loader.py``): one Pydantic type per
    file, no separate ``schemas`` module unless the type proves
    shared. :attr:`ok` distinguishes success from failure so callers
    can branch without parsing :attr:`text`; :attr:`error` is set on
    failure for callers that want to surface the cause.
    """

    ok: bool = Field(default=True, description="True when the skill ran successfully.")
    text: str = Field(default="", description="Assistant-visible response text.")
    error: str | None = Field(default=None, description="Failure reason when ok=False.")


class SkillExecutor:
    """Executes skills inline or in a forked sub-agent.

    Returns a :class:`SkillResult` rather than a bare ``str`` so
    callers can distinguish success from a surfaced error without
    parsing the response text. The success path renders through
    :class:`ResponseTextExtractor` (same path the model turn uses),
    keeping assistant-visible output normalized across the app.
    """

    def __init__(self, pool: "AgentPool", settings: "Settings", session_id: str = ""):
        self.pool = pool
        self.settings = settings
        self.session_id = session_id

    async def execute(self, skill: "SkillDefinition", arguments: str = "") -> SkillResult:
        """Execute a skill.

        Args:
            skill: The skill definition.
            arguments: Arguments passed to the skill.

        Returns:
            A :class:`SkillResult` with ``ok=True`` and ``text`` set
            on success, or ``ok=False`` with ``error`` describing the
            failure.
        """
        rendered = skill.render(arguments, session_id=self.session_id)

        if skill.context == "fork" and skill.agent:
            agent_name = skill.agent
            missing_message = f"Error: Agent '{agent_name}' not found for skill '{skill.name}'."
        else:
            agent_name = self.settings.skills.default_agent
            missing_message = f"Error: No '{agent_name}' agent available for skill '{skill.name}'."

        return await self._run_with_agent(
            agent_name=agent_name,
            prompt=rendered,
            skill=skill,
            missing_message=missing_message,
        )

    async def _run_with_agent(
        self,
        *,
        agent_name: str,
        prompt: str,
        skill: "SkillDefinition",
        missing_message: str,
    ) -> SkillResult:
        """Resolve ``agent_name`` from the pool and run ``prompt`` against it.

        Centralizes the agent-resolution + ``arun`` try/except that
        used to live in the two parallel ``_execute_forked`` /
        ``_execute_inline`` methods. ``KeyError`` (unknown agent) is
        handled separately so we can return a precise message; the
        narrow ``RuntimeError`` / ``ValueError`` / ``OSError`` catch
        covers expected agent-runtime failures. Programming bugs
        (``AttributeError``, ``TypeError``, …) intentionally propagate
        — they're not the executor's job to flatten.
        """
        try:
            agent = self.pool.get(agent_name)
        except KeyError:
            return SkillResult(ok=False, text=missing_message, error=missing_message)

        try:
            response = await agent.arun(prompt)
        except (RuntimeError, ValueError, OSError) as e:
            message = f"Error executing skill '{skill.name}': {e}"
            return SkillResult(ok=False, text=message, error=str(e))

        return SkillResult(ok=True, text=extract_response_text(response))

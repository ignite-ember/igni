"""The system prompt keeps the layout igni-1.0 was trained on (Agno 2.6's).

Agno 3 renders an instruction list differently — multi-line instructions
are no longer bulleted, sections gain blank lines — and moved the
user-memory guidance out of ``<user_memory>``. The expected strings below
are what Agno 2.6.22 produced for the same input, byte for byte
(``core/agents/trained_layout.py``, ``core/learn.py``).
"""

from __future__ import annotations

from agno.agent import Agent
from agno.learn.config import LearningMode, UserMemoryConfig
from agno.models.openai.like import OpenAILike

from ember_code.core.agents.trained_layout import TrainedLayout
from ember_code.core.learn import TrainedUserMemoryStore

BASE = "You are igni.\n\n## Working with the User\n\nAsk when unsure."
SKILLS = "Available skills:\n- /pr"
PLAN = "PLAN MODE — think first"


def _system(instructions: list[str], *, description: str | None = None) -> str:
    rendered, markdown = TrainedLayout.instructions(instructions, markdown=True)
    agent = Agent(
        model=OpenAILike(id="m", api_key="k", base_url="http://127.0.0.1:9"),
        description=description,
        instructions=rendered,
        markdown=markdown,
        telemetry=False,
    )
    message = agent.get_system_message(session=None)  # type: ignore[arg-type]
    assert message is not None
    return str(message.content)


def test_several_instructions_are_bulleted_and_followed_by_the_markdown_line() -> None:
    assert _system([BASE, SKILLS, PLAN]) == (
        f"- {BASE}\n- {SKILLS}\n- {PLAN}\n"
        "<additional_information>\n- Use markdown to format your answers.\n"
        "</additional_information>"
    )


def test_a_description_comes_first_as_before() -> None:
    assert _system([BASE, PLAN], description="Deeply analyzes code.") == (
        f"Deeply analyzes code.\n- {BASE}\n- {PLAN}\n"
        "<additional_information>\n- Use markdown to format your answers.\n"
        "</additional_information>"
    )


def test_one_instruction_is_left_to_agno_which_renders_it_as_2_6_did() -> None:
    assert TrainedLayout.instructions([BASE], markdown=True) == ([BASE], True)
    assert _system([BASE]) == (
        f"{BASE}\n\n<additional_information>\n- Use markdown to format your answers.\n"
        "</additional_information>"
    )


def test_the_user_memory_block_carries_its_guidance_inside() -> None:
    store = TrainedUserMemoryStore(
        config=UserMemoryConfig(
            mode=LearningMode.AGENTIC, enable_agent_tools=True, agent_can_update_memories=True
        )
    )
    assert store.instructions() == ""
    assert store.build_context(None) == (
        "<user_memory>\nNo memories saved about this user yet.\n\n"
        "Use `update_user_memory` to save observations, preferences, and context about "
        "this user that would help personalize future conversations or avoid asking the "
        "same questions.\n</user_memory>"
    )

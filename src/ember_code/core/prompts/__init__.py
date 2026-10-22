"""Prompt templates — loads prompt files from the prompts directory."""

from pathlib import Path

from ember_code.core.prompts.fragments import PromptFragments, fragments

__all__ = [
    "CODEINDEX_SCHEMA_PLACEHOLDER",
    "PROMPTS_DIR",
    "PromptFragments",
    "fragments",
    "inject_codeindex_schema",
    "load_prompt",
]

PROMPTS_DIR = Path(__file__).parent

# Prompts that teach an agent to query the CodeIndex write this token where the
# graph schema belongs, and it is replaced at load time with the one
# authoritative description.
#
# Why a placeholder rather than prose in each prompt: the schema was being
# maintained twice — ``neo4j_schema.GRAPH_SCHEMA_DESCRIPTION``, which the
# evaluation injected and measured at 85.8% against ripgrep's 64.5%, and a
# hand-written copy inside ``agents/data-architect.codeindex.md`` that the
# product actually shipped. The shipped copy knew about none of the counted
# facts the score came from — no ``fan_in``, no ``method_count``, no
# ``is_callable``, no ``sink_hits``, no term search — and still advertised two
# properties that are empty on all 86,332 indexed items. One source, substituted
# in, cannot drift like that.
#
# It is now one anchor among however many the installed extensions supply —
# see ``prompts.fragments``. The name is unchanged, which is the point: a
# prompt written against this token keeps working when the thing filling it
# stops being compiled in and starts being installed.
CODEINDEX_SCHEMA_PLACEHOLDER = "{{CODEINDEX_GRAPH_SCHEMA}}"


def inject_codeindex_schema(text: str) -> str:
    """Fill every prompt anchor in ``text``.

    Named for the one anchor that existed when it was written. Kept
    under that name because the call sites read well and the behaviour
    for that anchor is unchanged; what it does now is ask the fragment
    table, which may hold anchors from any installed extension.

    A no-op for text with no anchors, so it is safe to call on every
    prompt and every agent body.
    """
    return fragments.expand(text)


def load_prompt(name: str) -> str:
    """Load a prompt template by name (without extension).

    Raises ``FileNotFoundError`` if the prompt file does not exist.
    """
    path = PROMPTS_DIR / f"{name}.md"
    return inject_codeindex_schema(path.read_text().strip())

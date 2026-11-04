"""Prompt templates — loads prompt files from the prompts directory."""

from pathlib import Path

from ember_code.core.prompts.fragments import PromptFragments, fragments

__all__ = [
    "PROMPTS_DIR",
    "PromptFragments",
    "expand_anchors",
    "fragments",
    "load_prompt",
]

PROMPTS_DIR = Path(__file__).parent


def expand_anchors(text: str) -> str:
    """Fill every ``{{ANCHOR}}`` an installed extension supplies.

    A no-op for text with no anchors, so it is safe to call on every
    prompt and every agent body. See :mod:`ember_code.core.prompts.fragments`.
    """
    return fragments.expand(text)


def load_prompt(name: str) -> str:
    """Load a prompt template by name (without extension).

    Raises ``FileNotFoundError`` if the prompt file does not exist.
    """
    path = PROMPTS_DIR / f"{name}.md"
    return expand_anchors(path.read_text().strip())

"""Prompt templates — loads prompt files from the prompts directory."""

from pathlib import Path

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
CODEINDEX_SCHEMA_PLACEHOLDER = "{{CODEINDEX_GRAPH_SCHEMA}}"


def inject_codeindex_schema(text: str) -> str:
    """Replace the schema placeholder with the live graph description.

    A no-op for text without the placeholder, so it is safe to call on every
    prompt and every agent body. Imported lazily because the schema module pulls
    in the code_index package, and the prompt loader is used in contexts that
    have no index.
    """
    if CODEINDEX_SCHEMA_PLACEHOLDER not in text:
        return text
    from ember_code.core.code_index.neo4j_schema import GRAPH_SCHEMA_DESCRIPTION

    return text.replace(CODEINDEX_SCHEMA_PLACEHOLDER, GRAPH_SCHEMA_DESCRIPTION)


def load_prompt(name: str) -> str:
    """Load a prompt template by name (without extension).

    Raises ``FileNotFoundError`` if the prompt file does not exist.
    """
    path = PROMPTS_DIR / f"{name}.md"
    return inject_codeindex_schema(path.read_text().strip())

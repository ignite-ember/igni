"""``docs/prompt-targets.json`` is every section and slot igni's prompts have.

It is what an extension author reads to know what ``main.workflow`` is,
and what igni server reads to tell a super admin that a bundle names a
target this igni does not have. Generated from the prompt files and held
to them here, because a list kept by hand is a list that is wrong.

To refresh it::

    python -c "from pathlib import Path; from ember_code.core.prompts.document import \\
PromptTargets; Path('docs/prompt-targets.json').write_text(PromptTargets.current(\\
Path('src/ember_code/core/prompts')).model_dump_json(indent=2) + '\\n')"
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ember_code.core.prompts import PROMPTS_DIR
from ember_code.core.prompts.document import PromptDocument, PromptTargets

REPO = Path(__file__).resolve().parents[1]
LISTED = REPO / "docs" / "prompt-targets.json"
SERVER_COPY = REPO.parent / "ember-server" / "app" / "services" / "identity" / "prompt_targets.json"


def test_the_list_is_what_the_prompts_have():
    assert PromptTargets.model_validate_json(LISTED.read_text()) == PromptTargets.current(
        PROMPTS_DIR
    )


def test_every_section_igni_has_is_marked():
    """Each ``##`` heading of the main prompt sits inside a section, so
    an extension can reach all of it — nothing of igni's is unreplaceable
    except by replacing the whole prompt."""
    text = (PROMPTS_DIR / "main_agent.md").read_text()
    document = PromptDocument(text)
    marked = "".join(
        text.split(f"<!-- section:{name} -->", 1)[1].split(f"<!-- /section:{name} -->", 1)[0]
        for name in document.sections()
    )
    headings = [line for line in text.splitlines() if line.startswith("## ")]
    unmarked = [h for h in headings if h not in marked and "nowhere to file" not in h]
    assert not unmarked


@pytest.mark.skipif(not SERVER_COPY.is_file(), reason="no ember-server checkout beside this one")
def test_igni_servers_copy_is_the_same():
    """igni server keeps a copy to check bundles against; it must not drift."""
    assert PromptTargets.model_validate_json(SERVER_COPY.read_text()) == PromptTargets.current(
        PROMPTS_DIR
    )

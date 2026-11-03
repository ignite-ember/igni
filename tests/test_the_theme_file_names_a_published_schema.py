"""docs/UI_FLOOR.md's theme example names a schema that exists, and passes it.

Its ``$schema`` named https://igni.sh/schema/theme-1.json, which never
resolved (as the manifest's did, C14). igni's site serves
docs/theme.schema.json at its ``$id``, like the manifest's schema.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import jsonschema
import pytest

DOCS = Path(__file__).parent.parent / "docs"


def _example() -> dict[str, object]:
    text = (DOCS / "UI_FLOOR.md").read_text(encoding="utf-8")
    block = re.search(r"```jsonc\n(.*?)```", text, re.S)
    assert block, "the theme example is gone from UI_FLOOR.md"
    # jsonc: drop the `  // …` comments after a value (not the `//` in a URL).
    return json.loads(re.sub(r"\s+//[^\n]*", "", block.group(1)))


def _schema() -> dict[str, object]:
    return json.loads((DOCS / "theme.schema.json").read_text(encoding="utf-8"))


def test_the_example_names_the_schemas_own_id_on_igni_s_site():
    schema = _schema()
    assert schema["$id"] == "https://ignite-ember.sh/schema/theme-1.json"
    assert _example()["$schema"] == schema["$id"]


def test_the_example_is_a_valid_theme():
    jsonschema.Draft202012Validator(_schema()).validate(_example())


@pytest.mark.parametrize(
    "icon",
    [
        {"viewBox": "0 0 16 16", "d": "<svg onload=alert(1)>"},
        {"viewBox": "0 0 16 16", "d": "M4 7V5", "href": "https://example.com/x.svg"},
    ],
)
def test_an_icon_is_path_data_only(icon):
    """§4.3: a theme file must not become an XSS vector into the host."""
    theme = {**_example(), "icons": {"lock": icon}}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(_schema()).validate(theme)

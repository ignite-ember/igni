"""The published manifest schema, and the example that has to satisfy it.

``docs/extension-manifest.schema.json`` is a contract an extension
author points their editor at. Nothing checked it, and the first
manifest written against it — the reference extension's own — broke it
in three places, every one of them a place where the schema and the
prose had drifted:

* ``sidecar`` had no ``image``, because nothing had ever had to say
  where a sidecar's bits come from;
* a ``commands`` entry carried a ``label``, which belongs to
  ``menu`` — the collision that made ``contributes.tools`` into
  ``contributes.menu`` in the first place;
* a ``promptFragments`` entry declared an anchor and no ``source``,
  which is a fragment with no text to substitute.

A schema an author's editor validates against, that the one shipped
example fails, is worse than no schema: it teaches the wrong shape with
autocomplete.
"""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from tests.manifest_corpus import BASE, REFUSALS

REPO = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO / "docs" / "extension-manifest.schema.json"
EXAMPLES = REPO / "examples"


@pytest.fixture(scope="module")
def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


@pytest.fixture(scope="module")
def validator(schema: dict) -> Draft202012Validator:
    return Draft202012Validator(schema)


def _manifests() -> list[Path]:
    return sorted(EXAMPLES.glob("*/manifest.json"))


def test_the_schema_is_a_schema(schema: dict) -> None:
    """A malformed schema validates everything, silently."""
    Draft202012Validator.check_schema(schema)


def test_there_is_at_least_one_example() -> None:
    """Guards the parametrisation below from passing on an empty set."""
    assert _manifests(), f"no manifest.json under {EXAMPLES}"


@pytest.mark.parametrize("path", _manifests(), ids=lambda p: p.parent.name)
def test_an_example_manifest_validates(path: Path, validator: Draft202012Validator) -> None:
    errors = sorted(validator.iter_errors(json.loads(path.read_text())), key=str)
    assert not errors, "\n".join(
        f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}" for e in errors
    )


@pytest.mark.parametrize("path", _manifests(), ids=lambda p: p.parent.name)
def test_an_example_points_at_the_schema(path: Path, schema: dict) -> None:
    """So an author's editor completes and checks as they type.

    The ``$schema`` line is the whole delivery mechanism. A schema
    nobody references is documentation that happens to be JSON.
    """
    assert json.loads(path.read_text()).get("$schema") == schema["$id"]


# ── What the schema has to refuse ──────────────────────────────────
#
# A schema is only worth the shapes it rejects. The corpus lives in
# ``tests/manifest_corpus.py`` because the Pydantic model the backend
# installs through is held to the same cases from the other side — a
# manifest an author's editor accepts and the installer rejects is a
# support ticket, and the reverse is a permission somebody believes
# they declared.


@pytest.mark.parametrize(("name", "manifest", "where"), REFUSALS, ids=[c[0] for c in REFUSALS])
def test_the_schema_refuses(
    name: str, manifest: dict, where: str, validator: Draft202012Validator
) -> None:
    errors = list(validator.iter_errors(manifest))
    assert errors, f"{name}: accepted, and should not have been"
    paths = {"/".join(map(str, e.absolute_path)) for e in errors}
    assert where in paths, f"{name}: complained at {sorted(paths)}, wanted {where!r}"


def test_the_base_manifest_is_valid(validator: Draft202012Validator) -> None:
    """The refusal cases are edits to one valid manifest.

    If that base were itself invalid, every case above would pass for
    the wrong reason and none of them would be testing what it says.
    """
    assert not list(validator.iter_errors(BASE))


def test_the_reference_manifest_would_fail_if_broken(
    validator: Draft202012Validator,
) -> None:
    """The positive cases above pass on an empty error list.

    So this pins that the validator is actually looking at the file:
    take one required key out of the real manifest and it must object.
    """
    manifest = json.loads((EXAMPLES / "reference-extension" / "manifest.json").read_text())
    assert not list(validator.iter_errors(manifest))
    del manifest["contract"]
    assert list(validator.iter_errors(manifest))

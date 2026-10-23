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
# A schema is only worth the shapes it rejects. Each case below is a
# mistake that was either already made or is one keystroke away, and
# each asserts the *location* of the complaint as well as the fact of
# it: an error reported against the wrong path sends an author to the
# wrong line.

BASE = {
    "id": "acme.scanner",
    "version": "1.0.0",
    "contract": ">=1.0 <2",
    "trust": "unsigned",
    "sidecar": {"image": "acme__scanner:1.0.0", "mcp": {"transport": "stdio"}},
}


def _with(**overrides: object) -> dict:
    return {**BASE, **overrides}


@pytest.mark.parametrize(
    ("name", "manifest", "where"),
    [
        (
            "a sidecar with nowhere to come from",
            _with(sidecar={"mcp": {"transport": "stdio"}}),
            "sidecar",
        ),
        (
            "both packagings at once",
            _with(endpoint={"url": "https://scanner.internal/mcp"}),
            "",
        ),
        (
            "neither packaging",
            {k: v for k, v in BASE.items() if k != "sidecar"},
            "",
        ),
        (
            "a signed trust tier with no signature",
            _with(trust="partner"),
            "",
        ),
        (
            "an id that is not reverse-DNS",
            _with(id="scanner"),
            "id",
        ),
        (
            "a version that is not semver",
            _with(version="1.0"),
            "version",
        ),
        (
            "a command without its leading slash",
            _with(contributes={"commands": [{"command": "compliance", "desc": "x"}]}),
            "contributes/commands/0/command",
        ),
        (
            "a menu entry misfiled as a command",
            _with(contributes={"commands": [{"label": "C", "command": "/c", "desc": "x"}]}),
            "contributes/commands/0",
        ),
        (
            "a prompt fragment with no text",
            _with(contributes={"promptFragments": [{"anchor": "ACME_POLICY"}]}),
            "contributes/promptFragments/0",
        ),
        (
            "an anchor in the wrong case",
            _with(contributes={"promptFragments": [{"anchor": "acme_policy", "source": "file"}]}),
            "contributes/promptFragments/0/anchor",
        ),
        (
            "an rpc pattern that is not a prefix",
            _with(permissions={"rpc": ["ext.*.scanner.list"]}),
            "permissions/rpc/0",
        ),
        (
            "an egress entry that is a URL rather than a host",
            _with(permissions={"egress": ["https://ledger.internal"]}),
            "permissions/egress/0",
        ),
        (
            "a capability named as a product",
            _with(
                sidecar={
                    "image": "acme__scanner:1.0.0",
                    "mcp": {"transport": "stdio"},
                    "requires": [{"capability": "postgres", "constraint": ">=14"}],
                }
            ),
            "sidecar/requires/0/capability",
        ),
        (
            "a typo in a top-level key",
            _with(permissons={"workspace": "read"}),
            "",
        ),
        (
            "a page kind that is not namespaced",
            _with(contributes={"pages": [{"kind": "scan", "label": "Scan"}]}),
            "contributes/pages/0/kind",
        ),
    ],
)
def test_the_schema_refuses(
    name: str, manifest: dict, where: str, validator: Draft202012Validator
) -> None:
    errors = list(validator.iter_errors(manifest))
    assert errors, f"{name}: accepted, and should not have been"
    paths = {"/".join(map(str, e.absolute_path)) for e in errors}
    assert where in paths, f"{name}: complained at {sorted(paths)}, wanted {where!r}"


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

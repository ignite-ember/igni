"""One contract, stated twice, held to itself.

``docs/extension-manifest.schema.json`` is what an extension author
writes against and what their editor checks.
:class:`~ember_code.extensions.manifest.ExtensionManifest` is what the
backend installs through. Both describe the same document, and
nothing but this file stops them diverging.

The cost of divergence is asymmetric and both directions are bad. A
manifest the editor accepts and the installer rejects is an author
staring at a green file and a red install. A manifest the editor
rejects and the installer accepts is worse: a field that looks
declared, is not checked by anyone, and is probably a permission.

This repository has had this exact shape go wrong twice already — a
published stylesheet that lost track of the app's class names, and
token fallbacks that stopped matching the theme they were copied
from. Both looked fine in the one environment anybody tested.

Two checks, because each catches what the other cannot:

* **structural** — every schema property has a model field and back
  again, walked recursively. Catches a field nobody wrote a case for.
* **behavioural** — the shared corpus in ``tests/manifest_corpus.py``
  must be accepted or refused the same way by both. Catches a field
  that exists on both sides and means different things.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ValidationError

from ember_code.extensions.manifest import (
    CommandContribution,
    Contributes,
    DevCommand,
    Endpoint,
    EndpointAuth,
    ExtensionManifest,
    IndicatorContribution,
    McpBinding,
    MenuContribution,
    PageContribution,
    Permissions,
    PromptFragment,
    Requirement,
    Setting,
    Sidecar,
)
from tests.manifest_corpus import BASE, REFUSALS

REPO = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO / "docs" / "extension-manifest.schema.json"
EXAMPLES = REPO / "examples"

SCHEMA: dict[str, Any] = json.loads(SCHEMA_PATH.read_text())
VALIDATOR = Draft202012Validator(SCHEMA)

#: Where in the schema each model lives, as a path of property names.
#: Spelled out rather than discovered: a walker that guessed the
#: pairing could pair nothing and pass.
PAIRS: list[tuple[str, type[BaseModel]]] = [
    ("", ExtensionManifest),
    ("sidecar", Sidecar),
    ("sidecar.mcp", McpBinding),
    ("sidecar.requires[]", Requirement),
    ("dev", DevCommand),
    ("endpoint", Endpoint),
    ("endpoint.auth", EndpointAuth),
    ("permissions", Permissions),
    ("contributes", Contributes),
    ("contributes.pages[]", PageContribution),
    ("contributes.menu[]", MenuContribution),
    ("contributes.commands[]", CommandContribution),
    ("contributes.indicators[]", IndicatorContribution),
    ("contributes.promptFragments[]", PromptFragment),
    ("contributes.settings[]", Setting),
]


def _schema_at(path: str) -> dict[str, Any]:
    """The subschema at a dotted path, where ``[]`` descends into
    an array's ``items``."""
    node = SCHEMA
    if path:
        for step in path.split("."):
            array = step.endswith("[]")
            node = node["properties"][step.removesuffix("[]")]
            if array:
                node = node["items"]
    return node


def _model_keys(model: type[BaseModel]) -> set[str]:
    """Field names as they appear on the wire — the alias where one
    is set, since that is what JSON carries."""
    return {f.alias or name for name, f in model.model_fields.items()}


@pytest.mark.parametrize("path,model", PAIRS, ids=[p or "<root>" for p, _ in PAIRS])
def test_the_same_fields_exist_on_both_sides(path: str, model: type[BaseModel]) -> None:
    schema_keys = set(_schema_at(path).get("properties", {}))
    model_keys = _model_keys(model)
    assert schema_keys == model_keys, (
        f"at {path or '<root>'}: "
        f"only in the schema {sorted(schema_keys - model_keys)}, "
        f"only in the model {sorted(model_keys - schema_keys)}"
    )


@pytest.mark.parametrize("path,model", PAIRS, ids=[p or "<root>" for p, _ in PAIRS])
def test_both_sides_require_the_same_fields(path: str, model: type[BaseModel]) -> None:
    schema_required = set(_schema_at(path).get("required", []))
    model_required = {f.alias or name for name, f in model.model_fields.items() if f.is_required()}
    assert schema_required == model_required, (
        f"at {path or '<root>'}: "
        f"required only by the schema {sorted(schema_required - model_required)}, "
        f"required only by the model {sorted(model_required - schema_required)}"
    )


@pytest.mark.parametrize("path,model", PAIRS, ids=[p or "<root>" for p, _ in PAIRS])
def test_both_sides_refuse_unknown_fields(path: str, model: type[BaseModel]) -> None:
    """``additionalProperties: false`` on one side and ``extra="forbid"``
    on the other. A dropped key is a permission the author believes
    they declared."""
    assert _schema_at(path).get("additionalProperties") is False, (
        f"{path or '<root>'} does not set additionalProperties: false"
    )
    assert model.model_config.get("extra") == "forbid", (
        f"{model.__name__} does not forbid extra fields"
    )


def test_the_pairing_covers_every_object_in_the_schema() -> None:
    """Guards :data:`PAIRS` itself.

    A nested object added to the schema and not to the pairing would
    be unchecked, and every test above would still pass.
    """

    def walk(node: dict[str, Any], path: str) -> set[str]:
        found: set[str] = set()
        if node.get("type") == "object" and "properties" in node:
            found.add(path)
            for key, child in node["properties"].items():
                found |= walk(child, f"{path}.{key}" if path else key)
        if node.get("type") == "array" and isinstance(node.get("items"), dict):
            found |= walk(node["items"], f"{path}[]")
        return found

    assert walk(SCHEMA, "") == {p for p, _ in PAIRS}


# ── Behaviour: the same documents, the same verdicts ───────────────


def _model_accepts(manifest: dict[str, Any]) -> bool:
    try:
        ExtensionManifest.model_validate(manifest)
    except ValidationError:
        return False
    except ValueError:
        # ``model_validator`` raising a plain ValueError is wrapped by
        # pydantic, so this is belt and braces rather than a real path.
        return False
    return True


def test_both_sides_accept_the_base() -> None:
    assert not list(VALIDATOR.iter_errors(BASE))
    assert _model_accepts(BASE)


@pytest.mark.parametrize(("name", "manifest", "where"), REFUSALS, ids=[c[0] for c in REFUSALS])
def test_the_model_refuses_what_the_schema_refuses(
    name: str, manifest: dict[str, Any], where: str
) -> None:
    assert list(VALIDATOR.iter_errors(manifest)), f"{name}: the schema accepted it"
    assert not _model_accepts(manifest), f"{name}: the model accepted it"


@pytest.mark.parametrize(
    "path", sorted(EXAMPLES.glob("*/manifest.json")), ids=lambda p: p.parent.name
)
def test_the_model_accepts_every_example(path: Path) -> None:
    """The examples are validated against the schema elsewhere. This
    is the other half: an author's working manifest must install."""
    ExtensionManifest.load(path)


def test_a_round_trip_keeps_what_it_was_given() -> None:
    """Dumping by alias reproduces the file.

    An install that rewrites a manifest — to record grants, say — must
    not quietly drop `$schema` or rename `from`, both of which are
    aliased fields and both of which a naive dump loses.
    """
    raw = json.loads((EXAMPLES / "reference-extension" / "manifest.json").read_text())
    dumped = ExtensionManifest.model_validate(raw).model_dump(
        by_alias=True, exclude_none=True, exclude_defaults=True, mode="json"
    )
    assert not list(VALIDATOR.iter_errors(dumped))
    assert dumped["$schema"] == raw["$schema"]

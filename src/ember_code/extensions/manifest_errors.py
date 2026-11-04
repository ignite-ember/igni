"""A manifest's validation errors, as an author reads them.

pydantic's report names the model class, repeats the input's repr, tags
each line with an error type and links errors.pydantic.dev. Every part
of that is about the validator. An author wants the field, in the
manifest's own spelling, and what to write there — and, for a field
that does not exist, the one they probably meant.
"""

from __future__ import annotations

import difflib
import re
import types
import typing
from collections.abc import Iterable
from typing import Any, Union

from pydantic import BaseModel, ValidationError

#: What to write, for the fields people get wrong first. Keyed by the
#: path with list indices dropped.
_FIXES: dict[str, str] = {
    "id": 'reverse-DNS and lower case, e.g. "acme.tickets"',
    "version": 'a semantic version, e.g. "0.1.0"',
    "contract": 'the contract range it works against, e.g. ">=1.0 <2"',
    "trust": 'one of "unsigned", "partner", "first-party"',
    "permissions.tools": (
        'each tool is an object: {"name": "<id>.<tool>", "annotations": '
        '{"readOnlyHint": …, "destructiveHint": …, "idempotentHint": …, "openWorldHint": …}}'
    ),
    "permissions.tools.annotations": (
        "every tool states all four MCP hints — readOnlyHint, destructiveHint, "
        "idempotentHint, openWorldHint — because igni's permission modes decide on them"
    ),
    "permissions.rpc": 'backend methods the page may call, e.g. "ext.<id>.<tool>"',
}


class ManifestError(ValueError):
    """A manifest that does not validate, said field by field."""


class ManifestErrors:
    """Turns a :class:`ValidationError` into lines naming field and fix."""

    def __init__(self, model: type[BaseModel]) -> None:
        self._model = model

    def describe(self, error: ValidationError) -> str:
        lines = [self._line(detail) for detail in error.errors(include_url=False)]
        unique = list(dict.fromkeys(lines))
        if len(unique) == 1:
            return unique[0]
        return f"{len(unique)} problems:\n" + "\n".join(f"  - {line}" for line in unique)

    # ── one error ─────────────────────────────────────────────────

    def _line(self, detail: Any) -> str:
        loc = tuple(detail.get("loc", ()))
        kind = detail.get("type", "")
        where = self._path(loc)
        fix = self._fix(loc)

        if kind == "extra_forbidden":
            parent = self._model_at(loc[:-1])
            known = self._keys(parent) if parent is not None else []
            name = str(loc[-1])
            close = difflib.get_close_matches(name, known, n=1, cutoff=0.6)
            hint = f" — did you mean {close[0]!r}?" if close else ""
            listing = f" Known here: {', '.join(known)}." if known and not close else ""
            return f"{where}: not a field igni knows{hint}{listing}"
        if kind == "missing":
            return f"{where}: required" + (f" — {fix}" if fix else "")
        message = str(detail.get("msg", "invalid"))
        # A rule a validator states in a sentence of its own.
        for prefix in ("Value error, ", "Assertion failed, "):
            if message.startswith(prefix):
                message = message[len(prefix) :]
        if kind in {"model_type", "model_attributes_type", "dict_type"} and fix:
            message = "expected an object"
        if kind in {"enum", "literal_error"} and isinstance(detail.get("input"), str):
            expected = re.findall(r"'([^']*)'", str(detail.get("ctx", {}).get("expected", "")))
            close = difflib.get_close_matches(detail["input"], expected, n=1, cutoff=0.6)
            if close:
                message = f"{detail['input']!r} is not one igni knows — did you mean {close[0]!r}?"
        line = f"{where}: {message}" if where else message
        if fix and kind not in {"value_error", "assertion_error"}:
            line += f" — {fix}"
        return line

    @staticmethod
    def _path(loc: Iterable[Any]) -> str:
        out = ""
        for part in loc:
            if isinstance(part, int):
                out += f"[{part}]"
            else:
                out += f".{part}" if out else str(part)
        return out

    @staticmethod
    def _fix(loc: tuple[Any, ...]) -> str:
        key = ".".join(str(part) for part in loc if not isinstance(part, int))
        # The nearest enclosing field with advice: a missing hint is
        # about the annotations it belongs to.
        while key:
            if key in _FIXES:
                return _FIXES[key]
            key = key.rpartition(".")[0]
        return ""

    # ── the model, walked by the manifest's own keys ──────────────

    def _model_at(self, loc: tuple[Any, ...]) -> type[BaseModel] | None:
        current: Any = self._model
        for part in loc:
            if isinstance(part, int):
                continue
            if not (isinstance(current, type) and issubclass(current, BaseModel)):
                return None
            field = self._field(current, str(part))
            if field is None:
                return None
            current = self._inner_model(field.annotation)
        if isinstance(current, type) and issubclass(current, BaseModel):
            return current
        return None

    @staticmethod
    def _field(model: type[BaseModel], key: str) -> Any:
        for name, field in model.model_fields.items():
            if key in (name, field.alias):
                return field
        return None

    @classmethod
    def _inner_model(cls, annotation: Any) -> Any:
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            return annotation
        origin = typing.get_origin(annotation)
        if origin is typing.Annotated:
            return cls._inner_model(typing.get_args(annotation)[0])
        if origin in (Union, types.UnionType, list, tuple, set, frozenset, dict):
            for arg in typing.get_args(annotation):
                found = cls._inner_model(arg)
                if found is not None:
                    return found
        return None

    @staticmethod
    def _keys(model: type[BaseModel]) -> list[str]:
        return [field.alias or name for name, field in model.model_fields.items()]


__all__ = ["ManifestError", "ManifestErrors"]

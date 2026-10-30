"""The schema's types as Pydantic validators, for the host's own checks.

The SDKs get generated classes (:mod:`.codegen`). The host needs the
same types at runtime to check what an extension sends — a parameter of
the wrong shape is refused with :attr:`~.spec.ErrorCode.INVALID` before
any of igni is asked — and, in tests, to check that what the host
answers is what the schema promised.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError, create_model

from ember_code.extensions.host.spec import HostSchema, TypeExpr


class SchemaTypes:
    """Validators for type expressions, built once per schema."""

    def __init__(self, schema: HostSchema) -> None:
        self._schema = schema
        self._models: dict[str, type[BaseModel]] = {}
        self._adapters: dict[str, TypeAdapter[Any]] = {}

    def python_type(self, expression: str) -> Any:
        return self._build(TypeExpr.parse(expression))

    def adapter(self, expression: str) -> TypeAdapter[Any]:
        found = self._adapters.get(expression)
        if found is None:
            found = TypeAdapter(self.python_type(expression))
            self._adapters[expression] = found
        return found

    def validate(self, expression: str, value: Any) -> Any:
        """``value`` as the expression describes it, or :class:`ValidationError`."""
        return self.adapter(expression).validate_python(value)

    def check(self, expression: str, value: Any) -> str | None:
        """``None`` when ``value`` fits; otherwise why not."""
        try:
            self.validate(expression, value)
        except ValidationError as exc:
            return str(exc)
        return None

    def _build(self, expr: TypeExpr) -> Any:
        if expr.kind == "scalar":
            return {
                "str": str,
                "int": int,
                "float": float,
                "bool": bool,
                "None": type(None),
                "json": Any,
            }[expr.name]
        if expr.kind == "named":
            return self._model(expr.name)
        assert expr.item is not None
        inner = self._build(expr.item)
        if expr.kind == "list":
            return list[inner]  # type: ignore[valid-type]
        if expr.kind == "dict":
            return dict[str, inner]  # type: ignore[valid-type]
        return Optional[inner]  # noqa: UP045 — built at runtime from a value

    def _model(self, name: str) -> type[BaseModel]:
        found = self._models.get(name)
        if found is not None:
            return found
        spec = self._schema.type(name)
        if spec is None:
            raise KeyError(name)
        fields: dict[str, Any] = {}
        for f in spec.fields:
            annotation = self._build(TypeExpr.parse(f.type))
            if f.optional:
                fields[f.name] = (annotation, None)
            else:
                fields[f.name] = (annotation, ...)
        model = create_model(  # type: ignore[call-overload]
            name,
            __config__=ConfigDict(extra="allow", strict=False),
            **fields,
        )
        self._models[name] = model
        return model


__all__ = ["SchemaTypes"]

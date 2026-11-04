"""The vocabulary the host object model is written in.

``docs/HOST_OBJECT_MODEL.md`` describes igni to an extension as
**objects** with **properties**, **signals** and **methods**. This
module is the grammar of that description; :mod:`.definition` is the
description itself. Both SDKs are generated from it
(:mod:`.codegen`), the host implements it (:mod:`.model`), and the
coverage tests hold it against everything igni can do
(:mod:`.coverage`). One description, read four ways.

## Types

A member's type is a small expression rather than a Python class, so
that it can be printed as Python *and* as TypeScript without a
translator that knows Pydantic's internals:

* ``str``, ``int``, ``float``, ``bool``, ``None``;
* ``json`` — any JSON value, for a payload whose shape is the host's
  report of something rather than a promise about it;
* ``list[T]``, ``dict[str, T]``, ``T | None``;
* a named type from :attr:`HostSchema.types`.

## Stages

Every member is either built (``stage=1``) or defined and refused
(``stage=2``). A stage-2 member is in the schema and in both SDKs, so
an extension can be written against it today; calling it answers
:attr:`ErrorCode.STAGE_2` with a sentence saying why. It exists so a
member's shape can be decided once, before what backs it is built —
which is how the window's model was added: defined in 1.0.0, built in
1.1.0. No member is stage 2 today.

## Sessions other than the one on screen

An object marked ``per_session`` (``host.chat`` and the objects under
it) is the session on screen when it is reached as ``host.chat``, and
any session of the project when it is reached as
``host.sessions.chat(id)``. Every property of it reads any session. A
method acts on a session that is not on screen only when it names the
grant that takes (``elsewhere``) — a write somewhere the person is not
looking is broader than a write in front of them — and is refused there
otherwise.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

#: A member or object name: lowercase, underscores.
NAME = re.compile(r"^[a-z][a-z0-9_]*$")

#: An object path: names joined by dots (``chat.approvals``).
PATH = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")

#: The scalar type words.
SCALARS = frozenset({"str", "int", "float", "bool", "None", "json"})


class ErrorCode(StrEnum):
    """Why the host refused or failed, as a word code can branch on.

    The message beside it is a sentence for the extension's author; the
    code is for the extension's own logic. Both SDKs raise one error
    class carrying it.
    """

    #: The manifest does not grant the object (``permissions.host``).
    NOT_GRANTED = "not_granted"
    #: No such object, member or window.
    NOT_FOUND = "not_found"
    #: Parameters that do not fit the member's signature.
    INVALID = "invalid"
    #: A ``set`` on a property that is not writable.
    READ_ONLY = "read_only"
    #: Defined in the schema, not built yet. See the module docstring.
    STAGE_2 = "stage_2"
    #: The person could not have done this either, and neither can the
    #: extension — the host's own check said no.
    REFUSED = "refused"
    #: What answers it is not running on this machine and could not be
    #: started (igni's decision model). Nothing was done; try later.
    UNAVAILABLE = "unavailable"
    #: It was allowed and it went wrong.
    FAILED = "failed"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FieldSpec(Strict):
    """One field of a named type."""

    name: str
    type: str
    doc: str = ""
    #: ``True`` when the field may be left out; its default is then
    #: ``None`` for an optional type, or the empty value of the type.
    optional: bool = False


class TypeSpec(Strict):
    """A named value type: what a property holds or a signal carries."""

    name: str
    doc: str
    fields: tuple[FieldSpec, ...]

    @model_validator(mode="after")
    def _named(self) -> TypeSpec:
        if not re.match(r"^[A-Z][A-Za-z0-9]*$", self.name):
            raise ValueError(f"type {self.name!r} must be PascalCase")
        return self


class ParamSpec(Strict):
    name: str
    type: str
    doc: str = ""
    #: ``True`` for a parameter that may be left out. Its value is
    #: then ``None`` and the member's implementation decides what that
    #: means, which its ``doc`` says.
    optional: bool = False


class PropertySpec(Strict):
    """A value the extension can read, and sometimes write."""

    name: str
    type: str
    doc: str
    #: ``True`` when ``set`` is allowed. Writing is the same act the
    #: person's own UI performs, refused the same way.
    writable: bool = False
    #: The signals that report this property changed. A property with
    #: none is a constant for the life of the process — and is marked
    #: so rather than left unannounced. Names are this object's signals,
    #: or ``object.path.signal`` for another object's.
    changed_by: tuple[str, ...] = ()
    constant: bool = False
    #: Overrides the object's permission for this member.
    permission: str | None = None
    inherit_permission: bool = True
    stage: int = 1


class SignalSpec(Strict):
    """Something the host tells a subscriber happened, with the new value."""

    name: str
    type: str
    doc: str
    #: The name it travels under. Defaults to ``<object path>.<name>``;
    #: set for the four host events that predate the model, whose
    #: topic strings stay their wire names.
    wire: str | None = None
    permission: str | None = None
    inherit_permission: bool = True
    stage: int = 1


class MethodSpec(Strict):
    """Something the extension asks the host to do."""

    name: str
    params: tuple[ParamSpec, ...] = ()
    result: str = "None"
    doc: str
    permission: str | None = None
    inherit_permission: bool = True
    stage: int = 1
    #: For a method of a ``per_session`` object: the grant a call needs,
    #: besides its own, when it is addressed to a session that is not
    #: the one on screen (``host.sessions.chat(id)``). ``None``: it acts
    #: only on the session on screen. Naming the method's own grant
    #: means nothing more is needed there.
    elsewhere: str | None = None


class ObjectSpec(Strict):
    """One object of the model: ``host.<path>``."""

    path: str
    doc: str
    #: The grant that makes this object exist for an extension, or
    #: ``None`` for an object every extension has (its own state).
    permission: str | None
    #: ``True`` for objects that belong to a window — ``host.ui`` is
    #: the active window's, ``host.windows.ui(id)`` any one's.
    per_window: bool = False
    #: ``True`` for objects that belong to a session — ``host.chat`` is
    #: the session on screen, ``host.sessions.chat(id)`` any one's.
    per_session: bool = False
    properties: tuple[PropertySpec, ...] = ()
    signals: tuple[SignalSpec, ...] = ()
    methods: tuple[MethodSpec, ...] = ()

    def member(self, name: str) -> PropertySpec | SignalSpec | MethodSpec | None:
        for group in (self.properties, self.signals, self.methods):
            for member in group:
                if member.name == name:
                    return member
        return None

    def permission_of(self, member: PropertySpec | SignalSpec | MethodSpec) -> str | None:
        """The grant ``member`` needs: its own, else the object's."""
        if member.permission is not None or not member.inherit_permission:
            return member.permission
        return self.permission


class PermissionSpec(Strict):
    name: str
    #: What the person approving the install is told it allows.
    doc: str


class HostSchema(Strict):
    """The whole model, versioned.

    ``version`` is semver under the compatibility floor
    (EXTENSIBILITY §8): a new member is a minor, a changed or removed
    one is a major with a deprecation window.
    """

    version: str
    permissions: tuple[PermissionSpec, ...]
    types: tuple[TypeSpec, ...]
    objects: tuple[ObjectSpec, ...]

    # ── Lookup ────────────────────────────────────────────────────

    def object(self, path: str) -> ObjectSpec | None:
        for spec in self.objects:
            if spec.path == path:
                return spec
        return None

    def type(self, name: str) -> TypeSpec | None:
        for spec in self.types:
            if spec.name == name:
                return spec
        return None

    def resolve(
        self, member_path: str
    ) -> tuple[ObjectSpec, PropertySpec | SignalSpec | MethodSpec]:
        """``chat.post`` → (the ``chat`` object, its ``post`` method)."""
        owner, _, name = member_path.rpartition(".")
        spec = self.object(owner if owner else "")
        if spec is None:
            raise KeyError(member_path)
        member = spec.member(name)
        if member is None:
            raise KeyError(member_path)
        return spec, member

    def member_paths(self) -> list[str]:
        """Every member, as ``object.member`` (root members bare)."""
        out: list[str] = []
        for spec in self.objects:
            prefix = f"{spec.path}." if spec.path else ""
            for group in (spec.properties, spec.signals, spec.methods):
                out.extend(f"{prefix}{member.name}" for member in group)
        return out

    def signal_by_wire(self, wire: str) -> tuple[ObjectSpec, SignalSpec] | None:
        for spec in self.objects:
            for signal in spec.signals:
                if self.wire_name(spec, signal) == wire:
                    return spec, signal
        return None

    @staticmethod
    def wire_name(spec: ObjectSpec, signal: SignalSpec) -> str:
        if signal.wire is not None:
            return signal.wire
        return f"{spec.path}.{signal.name}" if spec.path else signal.name

    def permission_names(self) -> set[str]:
        return {p.name for p in self.permissions}

    def stage_two(self) -> list[str]:
        """Every member defined and not yet built."""
        out: list[str] = []
        for spec in self.objects:
            prefix = f"{spec.path}." if spec.path else ""
            for group in (spec.properties, spec.signals, spec.methods):
                out.extend(f"{prefix}{m.name}" for m in group if m.stage == 2)
        return out

    # ── Consistency ───────────────────────────────────────────────

    @model_validator(mode="after")
    def _consistent(self) -> HostSchema:
        problems: list[str] = []
        names = {t.name for t in self.types}
        if len(names) != len(self.types):
            problems.append("a type is defined twice")
        permissions = self.permission_names()
        paths = [o.path for o in self.objects]
        if len(set(paths)) != len(paths):
            problems.append("an object is defined twice")
        wires: dict[str, str] = {}

        def check_type(where: str, expression: str) -> None:
            for word in type_words(expression):
                if word not in SCALARS and word not in names:
                    problems.append(f"{where}: unknown type {word!r} in {expression!r}")

        for t in self.types:
            for f in t.fields:
                check_type(f"{t.name}.{f.name}", f.type)

        for spec in self.objects:
            if spec.path and not PATH.match(spec.path):
                problems.append(f"object path {spec.path!r} is not dotted lowercase")
            if spec.permission is not None and spec.permission not in permissions:
                problems.append(f"{spec.path}: permission {spec.permission!r} is not declared")
            seen: set[str] = set()
            signal_names = {s.name for s in spec.signals}
            for group in (spec.properties, spec.signals, spec.methods):
                for member in group:
                    where = f"{spec.path}.{member.name}" if spec.path else member.name
                    if not NAME.match(member.name):
                        problems.append(f"{where}: not a lowercase name")
                    if member.name in seen:
                        problems.append(f"{where}: defined twice")
                    seen.add(member.name)
                    if member.stage not in (1, 2):
                        problems.append(f"{where}: stage must be 1 or 2")
                    own = member.permission
                    if own is not None and own not in permissions:
                        problems.append(f"{where}: permission {own!r} is not declared")
            for prop in spec.properties:
                where = f"{spec.path}.{prop.name}"
                check_type(where, prop.type)
                if not prop.constant and not prop.changed_by:
                    problems.append(
                        f"{where}: every property has a change signal — name it in "
                        "changed_by, or mark the property constant"
                    )
                for ref in prop.changed_by:
                    if "." in ref:
                        target_path, _, target = ref.rpartition(".")
                        target_spec = next((o for o in self.objects if o.path == target_path), None)
                        if target_spec is None or target not in {
                            s.name for s in target_spec.signals
                        }:
                            problems.append(f"{where}: changed_by {ref!r} is not a signal")
                    elif ref not in signal_names:
                        problems.append(f"{where}: changed_by {ref!r} is not a signal")
            for signal in spec.signals:
                where = f"{spec.path}.{signal.name}" if spec.path else signal.name
                check_type(where, signal.type)
                wire = self.wire_name(spec, signal)
                if wire in wires:
                    problems.append(f"{where}: wire name {wire!r} also used by {wires[wire]}")
                wires[wire] = where
            if spec.per_window and spec.per_session:
                problems.append(f"{spec.path}: belongs to a window or a session, not both")
            for method in spec.methods:
                where = f"{spec.path}.{method.name}"
                check_type(where, method.result)
                if method.elsewhere is not None:
                    if not spec.per_session:
                        problems.append(f"{where}: elsewhere, on an object no session owns")
                    if method.elsewhere not in permissions:
                        problems.append(
                            f"{where}: elsewhere permission {method.elsewhere!r} is not declared"
                        )
                for param in method.params:
                    if not NAME.match(param.name):
                        problems.append(f"{where}({param.name}): not a lowercase name")
                    check_type(f"{where}({param.name})", param.type)
        if problems:
            raise ValueError("the host schema is inconsistent:\n  " + "\n  ".join(problems))
        return self


# ── Type expressions ──────────────────────────────────────────────


_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def type_words(expression: str) -> list[str]:
    """The type names an expression mentions, without ``list``/``dict``."""
    return [w for w in _TOKEN.findall(expression) if w not in ("list", "dict")]


class TypeExpr(BaseModel):
    """A parsed type expression."""

    kind: str  # scalar | named | list | dict | optional
    name: str = ""
    item: TypeExpr | None = None

    @classmethod
    def parse(cls, expression: str) -> TypeExpr:
        text = expression.strip()
        if text.endswith("| None"):
            inner = text[: -len("| None")].strip()
            return cls(kind="optional", item=cls.parse(inner))
        if text.startswith("list[") and text.endswith("]"):
            return cls(kind="list", item=cls.parse(text[5:-1]))
        if text.startswith("dict[str,") and text.endswith("]"):
            return cls(kind="dict", item=cls.parse(text[len("dict[str,") : -1]))
        if text in SCALARS:
            return cls(kind="scalar", name=text)
        if re.fullmatch(r"[A-Z][A-Za-z0-9]*", text):
            return cls(kind="named", name=text)
        raise ValueError(f"not a type expression: {expression!r}")


def field(name: str, type_: str, doc: str = "", *, optional: bool = False) -> FieldSpec:
    return FieldSpec(name=name, type=type_, doc=doc, optional=optional)


def param(name: str, type_: str, doc: str = "", *, optional: bool = False) -> ParamSpec:
    return ParamSpec(name=name, type=type_, doc=doc, optional=optional)


def prop(
    name: str,
    type_: str,
    doc: str,
    *,
    changed_by: tuple[str, ...] | list[str] = (),
    writable: bool = False,
    constant: bool = False,
    permission: str | None = None,
    always: bool = False,
    stage: int = 1,
) -> PropertySpec:
    return PropertySpec(
        name=name,
        type=type_,
        doc=doc,
        changed_by=tuple(changed_by),
        writable=writable,
        constant=constant,
        permission=permission,
        inherit_permission=not always,
        stage=stage,
    )


def signal(
    name: str,
    type_: str,
    doc: str,
    *,
    wire: str | None = None,
    permission: str | None = None,
    always: bool = False,
    stage: int = 1,
) -> SignalSpec:
    return SignalSpec(
        name=name,
        type=type_,
        doc=doc,
        wire=wire,
        permission=permission,
        inherit_permission=not always,
        stage=stage,
    )


def method(
    name: str,
    doc: str,
    *params: ParamSpec,
    result: str = "None",
    permission: str | None = None,
    always: bool = False,
    stage: int = 1,
    elsewhere: str | None = None,
) -> MethodSpec:
    return MethodSpec(
        name=name,
        doc=doc,
        params=tuple(params),
        result=result,
        permission=permission,
        inherit_permission=not always,
        stage=stage,
        elsewhere=elsewhere,
    )


def typedef(name: str, doc: str, *fields: FieldSpec) -> TypeSpec:
    return TypeSpec(name=name, doc=doc, fields=tuple(fields))


def schema_json(schema: HostSchema) -> dict[str, Any]:
    """The schema as plain JSON, for the generated SDKs to carry."""
    return schema.model_dump(mode="json")


__all__ = [
    "ErrorCode",
    "FieldSpec",
    "HostSchema",
    "MethodSpec",
    "ObjectSpec",
    "ParamSpec",
    "PermissionSpec",
    "PropertySpec",
    "SignalSpec",
    "TypeExpr",
    "TypeSpec",
    "field",
    "method",
    "param",
    "prop",
    "schema_json",
    "signal",
    "type_words",
    "typedef",
]

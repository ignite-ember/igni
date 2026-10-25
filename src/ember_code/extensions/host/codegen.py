"""Generate both SDKs from the host schema.

``docs/HOST_OBJECT_MODEL.md``: "One schema, two SDKs." The schema is
:data:`~ember_code.extensions.host.definition.HOST_SCHEMA`; this writes

* ``packages/igni-extension/src/igni_extension/host_model.py`` — the
  Python SDK's ``ext.host``: a typed Pydantic model per value type and a
  class per object, whose properties, signals and methods travel over
  the extension process's loopback socket;
* ``packages/extension-bridge/src/hostModel.generated.ts`` — the same
  for a page, as ``bridge.host``, over the bridge.

Neither file is edited by hand. ``tests/test_the_host_sdks_are_generated.py``
regenerates both in memory and fails when what is checked in differs, so
the schema and the SDKs cannot disagree. Run ``make host-sdks`` (or
``python scripts/generate_host_sdks.py``) after changing the schema.

Names are the schema's in both languages — ``bridge.host.chat.run_completed``,
not ``runCompleted`` — so a member is spelled one way everywhere it is
written down.
"""

from __future__ import annotations

from pathlib import Path

from ember_code.extensions.host.definition import HOST_SCHEMA
from ember_code.extensions.host.spec import HostSchema, MethodSpec, ObjectSpec, TypeExpr

#: Where the generated files live, relative to the repository root.
PYTHON_OUTPUT = Path("packages/igni-extension/src/igni_extension/host_model.py")
TYPESCRIPT_OUTPUT = Path("packages/extension-bridge/src/hostModel.generated.ts")

_BANNER = (
    "GENERATED from the igni host schema {version} by "
    "ember_code.extensions.host.codegen — do not edit. "
    "Run `make host-sdks` after changing the schema."
)


class ObjectNaming:
    """Class names and the tree of objects, shared by both generators."""

    def __init__(self, schema: HostSchema) -> None:
        self.schema = schema

    @staticmethod
    def class_name(path: str) -> str:
        if not path:
            return "Host"
        return "Host" + "".join(part.title().replace("_", "") for part in path.split("."))

    def children(self, path: str) -> list[ObjectSpec]:
        """Objects one level below ``path``, in schema order."""
        depth = 0 if not path else path.count(".") + 1
        out: list[ObjectSpec] = []
        for spec in self.schema.objects:
            if not spec.path or spec.path == path:
                continue
            if spec.path.count(".") != depth:
                continue
            if path and not spec.path.startswith(path + "."):
                continue
            out.append(spec)
        return out

    @staticmethod
    def attribute(spec: ObjectSpec) -> str:
        return spec.path.rpartition(".")[2]

    def per_window(self) -> list[ObjectSpec]:
        return [o for o in self.schema.objects if o.per_window]

    def check(self) -> None:
        """No child object may share a name with a member of its parent."""
        for spec in self.schema.objects:
            names = {m.name for g in (spec.properties, spec.signals, spec.methods) for m in g}
            for child in self.children(spec.path):
                if self.attribute(child) in names:
                    raise ValueError(
                        f"{child.path}: its name collides with a member of "
                        f"{spec.path or 'the root'}"
                    )

    def ordered(self) -> list[ObjectSpec]:
        """Deepest first, so a class is defined before its parent uses it."""
        return sorted(self.schema.objects, key=lambda o: -(o.path.count(".") + 1 if o.path else 0))


# ── Python ────────────────────────────────────────────────────────


class PythonGenerator:
    """``igni_extension.host_model``."""

    #: Schema types the Python SDK already has, hand-written with the
    #: limits igni enforces, and used rather than generated: an author
    #: who wrote ``LocalNotification`` before the model existed keeps
    #: writing it. ``tests/test_the_host_sdks_are_generated.py`` holds
    #: their fields to the schema's.
    EXTERNAL: dict[str, tuple[str, str]] = {
        "LocalNotification": ("igni_extension.notifications", "LocalNotification"),
        "NotificationTarget": ("igni_extension.notifications", "Target"),
    }

    def __init__(self, schema: HostSchema = HOST_SCHEMA) -> None:
        self.schema = schema
        self.naming = ObjectNaming(schema)

    def type(self, expression: str) -> str:
        return self._render(TypeExpr.parse(expression))

    def _render(self, expr: TypeExpr) -> str:
        if expr.kind == "scalar":
            return "JsonValue" if expr.name == "json" else expr.name
        if expr.kind == "named":
            return expr.name
        assert expr.item is not None
        inner = self._render(expr.item)
        if expr.kind == "list":
            return f"list[{inner}]"
        if expr.kind == "dict":
            return f"dict[str, {inner}]"
        return f"{inner} | None"

    def _default(self, expression: str) -> str:
        expr = TypeExpr.parse(expression)
        if expr.kind == "optional" or (expr.kind == "scalar" and expr.name == "json"):
            return "None"
        if expr.kind == "list":
            return "Field(default_factory=list)"
        if expr.kind == "dict":
            return "Field(default_factory=dict)"
        if expr.kind == "scalar":
            return {"str": '""', "int": "0", "float": "0.0", "bool": "False"}.get(expr.name, "None")
        return "None"

    @staticmethod
    def _doc(text: str, indent: str) -> list[str]:
        text = text.replace('"""', "'''").strip()
        if not text:
            return []
        return [f'{indent}"""{text}"""']

    def generate(self) -> str:
        self.naming.check()
        lines: list[str] = [
            f'"""{_BANNER.format(version=self.schema.version)}',
            "",
            "`ext.host` as an extension's process sees it. Read",
            "`docs/HOST_OBJECT_MODEL.md` in igni for what each object is.",
            '"""',
            "",
            "# ruff: noqa: E501",
            "",
            "from __future__ import annotations",
            "",
            "from typing import cast",
            "",
            "from pydantic import BaseModel, ConfigDict, Field",
            "",
            "from igni_extension.members import (",
            "    NO_RESULT,",
            "    HostLink,",
            "    HostObject,",
            "    JsonValue,",
            "    Property,",
            "    Signal,",
            ")",
        ]
        modules: dict[str, list[str]] = {}
        for module, name in self.EXTERNAL.values():
            modules.setdefault(module, []).append(name)
        for module, names in sorted(modules.items()):
            lines.append(f"from {module} import {', '.join(sorted(names))}")
        lines += ["", f'SCHEMA_VERSION = "{self.schema.version}"', ""]
        for schema_name, (_, name) in sorted(self.EXTERNAL.items()):
            if schema_name != name:
                lines.append(f"{schema_name} = {name}")
        for spec in self.schema.types:
            if spec.name in self.EXTERNAL:
                continue
            lines += ["", f"class {spec.name}(BaseModel):"]
            lines += self._doc(spec.doc, "    ")
            lines += ["", '    model_config = ConfigDict(extra="allow")', ""]
            for f in spec.fields:
                annotation = self.type(f.type)
                if f.optional:
                    lines.append(f"    {f.name}: {annotation} = {self._default(f.type)}")
                else:
                    lines.append(f"    {f.name}: {annotation}")
                lines += self._doc(f.doc, "    ")
            lines.append("")
        for obj in self.naming.ordered():
            lines += self._object(obj)
        lines += [
            "",
            "__all__ = [",
            *sorted(
                [f'    "{t.name}",' for t in self.schema.types]
                + [f'    "{self.naming.class_name(o.path)}",' for o in self.schema.objects]
                + ['    "SCHEMA_VERSION",']
            ),
            "]",
            "",
        ]
        return "\n".join(lines)

    def _object(self, spec: ObjectSpec) -> list[str]:
        name = self.naming.class_name(spec.path)
        out = ["", f"class {name}(HostObject):"]
        out += self._doc(spec.doc, "    ")
        out += [
            "",
            f'    PATH = "{spec.path}"',
            "",
            "    def __init__(self, link: HostLink, window: str | None = None) -> None:",
            "        super().__init__(link, window)",
        ]
        for child in self.naming.children(spec.path):
            out.append(
                f"        self.{self.naming.attribute(child)} = "
                f"{self.naming.class_name(child.path)}(link, window)"
            )
        for p in spec.properties:
            out.append(
                f"        self.{p.name}: Property[{self.type(p.type)}] = Property("
                f'self, "{p.name}", {self.type(p.type)}, writable={p.writable})'
            )
            out += self._doc(p.doc, "        ")
        for s in spec.signals:
            wire = self.schema.wire_name(spec, s)
            out.append(
                f"        self.{s.name}: Signal[{self.type(s.type)}] = Signal("
                f'"{wire}", {self.type(s.type)})'
            )
            out += self._doc(s.doc, "        ")
        for m in spec.methods:
            out += self._method(m)
        if spec.path == "windows":
            per_window = self.naming.per_window()
            for target in per_window:
                cls = self.naming.class_name(target.path)
                attr = self.naming.attribute(target)
                out += [
                    "",
                    f"    def {attr}(self, window_id: str) -> {cls}:",
                    f'        """`host.{target.path}` of one window, by its id."""',
                    f"        return {cls}(self._link, window_id)",
                ]
        return out

    def _method(self, m: MethodSpec) -> list[str]:
        params = ["self"]
        body_args: list[str] = []
        for p in m.params:
            annotation = self.type(p.type)
            if p.optional:
                if TypeExpr.parse(p.type).kind != "optional":
                    annotation = f"{annotation} | None"
                params.append(f"{p.name}: {annotation} = None")
            else:
                params.append(f"{p.name}: {annotation}")
            body_args.append(f'"{p.name}": {p.name}')
        result = self.type(m.result)
        signature = f"    async def {m.name}({', '.join(params)}) -> {result}:"
        call_args = "{" + ", ".join(body_args) + "}"
        out = ["", signature]
        out += self._doc(m.doc, "        ")
        if m.result == "None":
            out.append(f'        await self._call("{m.name}", {call_args}, NO_RESULT)')
        else:
            out.append(
                f'        return cast({result}, await self._call("{m.name}", {call_args}, {result}))'
            )
        return out


# ── TypeScript ────────────────────────────────────────────────────


class TypeScriptGenerator:
    """``hostModel.generated.ts`` in ``@igni/extension-bridge``."""

    def __init__(self, schema: HostSchema = HOST_SCHEMA) -> None:
        self.schema = schema
        self.naming = ObjectNaming(schema)

    def type(self, expression: str) -> str:
        return self._render(TypeExpr.parse(expression))

    def _render(self, expr: TypeExpr) -> str:
        if expr.kind == "scalar":
            return {
                "str": "string",
                "int": "number",
                "float": "number",
                "bool": "boolean",
                "None": "null",
                "json": "JsonValue",
            }[expr.name]
        if expr.kind == "named":
            return expr.name
        assert expr.item is not None
        inner = self._render(expr.item)
        if expr.kind == "list":
            return f"({inner})[]" if " " in inner else f"{inner}[]"
        if expr.kind == "dict":
            return f"Record<string, {inner}>"
        return f"{inner} | null"

    @staticmethod
    def _doc(text: str, indent: str) -> list[str]:
        text = text.replace("*/", "*\\/").strip()
        if not text:
            return []
        return [f"{indent}/** {text} */"]

    def generate(self) -> str:
        self.naming.check()
        lines = [
            f"// {_BANNER.format(version=self.schema.version)}",
            "//",
            "// `bridge.host` as an extension's page sees it. Read",
            "// `docs/HOST_OBJECT_MODEL.md` in igni for what each object is.",
            "",
            'import { HostObject, Property, Signal, type HostLink, type JsonValue } from "./hostModel";',
            "",
            f'export const SCHEMA_VERSION = "{self.schema.version}";',
            "",
        ]
        for spec in self.schema.types:
            lines += self._doc(spec.doc, "")
            lines.append(f"export interface {spec.name} {{")
            for f in spec.fields:
                lines += self._doc(f.doc, "  ")
                optional = "?" if f.optional else ""
                lines.append(f"  {f.name}{optional}: {self.type(f.type)};")
            lines += ["}", ""]
        for obj in self.naming.ordered():
            lines += self._object(obj)
        return "\n".join(lines) + "\n"

    def _object(self, spec: ObjectSpec) -> list[str]:
        name = self.naming.class_name(spec.path)
        out = self._doc(spec.doc, "")
        out.append(f"export class {name} extends HostObject {{")
        out.append(f'  static readonly PATH = "{spec.path}";')
        children = self.naming.children(spec.path)
        for child in children:
            out.append(
                f"  readonly {self.naming.attribute(child)}: {self.naming.class_name(child.path)};"
            )
        for p in spec.properties:
            out += self._doc(p.doc, "  ")
            out.append(f"  readonly {p.name}: Property<{self.type(p.type)}>;")
        for s in spec.signals:
            out += self._doc(s.doc, "  ")
            out.append(f"  readonly {s.name}: Signal<{self.type(s.type)}>;")
        out += [
            "",
            "  constructor(link: HostLink, window: string | null = null) {",
            f'    super(link, "{spec.path}", window);',
        ]
        for child in children:
            out.append(
                f"    this.{self.naming.attribute(child)} = new "
                f"{self.naming.class_name(child.path)}(link, window);"
            )
        for p in spec.properties:
            out.append(
                f'    this.{p.name} = new Property(this, "{p.name}", {str(p.writable).lower()});'
            )
        for s in spec.signals:
            wire = self.schema.wire_name(spec, s)
            out.append(f'    this.{s.name} = new Signal("{wire}");')
        out.append("  }")
        for m in spec.methods:
            out += self._method(m)
        if spec.path == "windows":
            for target in self.naming.per_window():
                cls = self.naming.class_name(target.path)
                attr = self.naming.attribute(target)
                out += [
                    "",
                    f"  /** `host.{target.path}` of one window, by its id. */",
                    f"  {attr}(windowId: string): {cls} {{",
                    f"    return new {cls}(this.link, windowId);",
                    "  }",
                ]
        out += ["}", ""]
        return out

    def _method(self, m: MethodSpec) -> list[str]:
        params: list[str] = []
        names: list[str] = []
        for p in m.params:
            ts = self.type(p.type)
            if p.optional:
                params.append(f"{p.name}?: {ts}")
            else:
                params.append(f"{p.name}: {ts}")
            names.append(p.name)
        result = "void" if m.result == "None" else self.type(m.result)
        args = "{ " + ", ".join(names) + " }" if names else "{}"
        out = [""]
        out += self._doc(m.doc, "  ")
        out.append(f"  {m.name}({', '.join(params)}): Promise<{result}> {{")
        out.append(f'    return this.call<{result}>("{m.name}", {args});')
        out.append("  }")
        return out


class HostSdks:
    """Both generated files, and whether what is on disk is current."""

    def __init__(self, root: Path, schema: HostSchema = HOST_SCHEMA) -> None:
        self.root = root
        self.schema = schema

    def outputs(self) -> dict[Path, str]:
        return {
            self.root / PYTHON_OUTPUT: PythonGenerator(self.schema).generate(),
            self.root / TYPESCRIPT_OUTPUT: TypeScriptGenerator(self.schema).generate(),
        }

    def write(self) -> list[Path]:
        written: list[Path] = []
        for path, text in self.outputs().items():
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                path.write_text(text, encoding="utf-8")
                written.append(path)
        return written

    def stale(self) -> list[Path]:
        """Generated files that do not match the schema."""
        return [
            path
            for path, text in self.outputs().items()
            if not path.exists() or path.read_text(encoding="utf-8") != text
        ]


__all__ = [
    "PYTHON_OUTPUT",
    "TYPESCRIPT_OUTPUT",
    "HostSdks",
    "ObjectNaming",
    "PythonGenerator",
    "TypeScriptGenerator",
]

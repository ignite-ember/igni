"""The code in the extension docs runs as it is printed.

A developer's first half hour with extensions is these documents, read
top to bottom and pasted. A block that raises ``NameError`` the moment
it is pasted teaches that the docs are not to be trusted, so every
fenced block here is one of four things, said by an HTML comment on the
line directly above its fence:

* nothing — a ``python`` block **runs**, standalone, in a fresh
  namespace (top-level ``await`` allowed);
* ``<!-- continues -->`` — it runs in the previous python block's
  namespace, for a walk-through split by prose;
* ``<!-- illustrative -->`` — an excerpt that cannot run on its own; it
  is only compiled;
* ``<!-- file: <path from the repository root> -->`` — a copy of a file
  in the repository, and it must equal that file exactly. That file is
  what runs (``examples/ticket-lookup`` has tests of its own), so the
  document cannot drift from it.

A ``json`` block that is a whole manifest (it has an ``id`` and a
``contract``) is validated against ``docs/extension-manifest.schema.json``.

Blocks that need the SDKs (``igni-extension``, ``igni-service``) skip
where they are not installed — igni's own environment does not carry
them — and run in CI's ``packages`` job, which installs both from the
checkout. The SDK READMEs follow the same convention and are checked by
their packages' own tests.

This module imports nothing from igni or its ``tests`` package, so it
runs in that job with ``--noconftest``.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DOCS = [
    REPO / "docs" / "EXTENSIBILITY.md",
    REPO / "docs" / "YOUR_FIRST_EXTENSION.md",
]
SCHEMA = REPO / "docs" / "extension-manifest.schema.json"

#: Modules a block may need that igni's own environment lacks.
SDK_MODULES = {"igni_extension", "igni_service", "fastapi", "httpx", "uvicorn"}

_FENCE = re.compile(r"^(\s*)```(\S*)\s*$")
_MARKER = re.compile(r"^\s*<!--\s*(illustrative|continues|file:\s*(\S+))\s*-->\s*$")


@dataclass
class Block:
    doc: Path
    line: int
    lang: str
    code: str
    marker: str | None  # "illustrative" | "continues" | "file" | None
    file: str | None

    @property
    def id(self) -> str:
        return f"{self.doc.name}:{self.line}"


def blocks(doc: Path) -> list[Block]:
    lines = doc.read_text().splitlines()
    found: list[Block] = []
    i = 0
    while i < len(lines):
        opening = _FENCE.match(lines[i])
        if not opening:
            i += 1
            continue
        indent, lang = opening.group(1), opening.group(2)
        marker = file = None
        if i > 0 and (m := _MARKER.match(lines[i - 1])):
            marker = "file" if m.group(2) else m.group(1)
            file = m.group(2)
        body: list[str] = []
        j = i + 1
        while j < len(lines) and not re.match(rf"^{indent}```\s*$", lines[j]):
            # A fence inside a list item is indented; its body is too.
            line = lines[j]
            body.append(line[len(indent) :] if line.startswith(indent) else line)
            j += 1
        found.append(Block(doc, i + 1, lang, "\n".join(body) + "\n", marker, file))
        i = j + 1
    return found


def _existing_docs() -> list[Path]:
    return [d for d in DOCS if d.is_file()]


def _all(lang: str | None = None) -> list[Block]:
    out = [b for d in _existing_docs() for b in blocks(d)]
    return [b for b in out if lang is None or b.lang == lang]


def _run(code: str, namespace: dict) -> None:
    compiled = compile(code, "<doc block>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    result = eval(compiled, namespace)  # noqa: S307 - the docs' own code, run on purpose
    if inspect.iscoroutine(result):
        asyncio.run(result)


def _python_runs() -> list[list[Block]]:
    """Python blocks grouped into chains: a block and the ones continuing it."""
    chains: list[list[Block]] = []
    for doc in _existing_docs():
        for block in blocks(doc):
            if block.lang != "python" or block.marker in ("illustrative", "file"):
                continue
            if block.marker == "continues":
                assert chains and chains[-1][0].doc == doc, (
                    f"{block.id} continues, and there is no python block before it"
                )
                chains[-1].append(block)
            else:
                chains.append([block])
    return chains


def test_the_docs_have_blocks() -> None:
    """Guards the parametrisations from passing on nothing."""
    assert _all("python"), "no python blocks found — has the fence parser broken?"


@pytest.mark.parametrize("block", _all("python"), ids=lambda b: b.id)
def test_every_python_block_compiles(block: Block) -> None:
    try:
        compile(block.code, block.id, "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    except SyntaxError as exc:
        pytest.fail(f"{block.id}: {exc}")


@pytest.mark.parametrize("chain", _python_runs(), ids=lambda c: c[0].id)
def test_a_python_block_runs_as_printed(chain: list[Block]) -> None:
    namespace: dict = {"__name__": "__doc_block__"}
    for block in chain:
        try:
            _run(block.code, namespace)
        except ModuleNotFoundError as exc:
            if (exc.name or "").split(".")[0] in SDK_MODULES:
                pytest.skip(f"{block.id} needs {exc.name}, which this environment lacks")
            raise AssertionError(f"{block.id}: {exc}") from exc
        except Exception as exc:
            raise AssertionError(
                f"{block.id} fails as printed ({type(exc).__name__}: {exc}). Fix it, "
                "or mark it <!-- illustrative --> if it is an excerpt."
            ) from exc


@pytest.mark.parametrize("block", [b for b in _all() if b.marker == "file"], ids=lambda b: b.id)
def test_a_file_shown_is_the_file_in_the_repository(block: Block) -> None:
    assert block.file is not None
    path = REPO / block.file
    assert path.is_file(), f"{block.id} shows {block.file}, which does not exist"
    assert block.code.rstrip("\n") == path.read_text().rstrip("\n"), (
        f"{block.id} differs from {block.file}; copy the file into the doc again"
    )


def _manifests() -> list[Block]:
    out = []
    for block in _all("json"):
        if block.marker == "illustrative":
            continue
        try:
            value = json.loads(block.code)
        except ValueError:
            continue  # a fragment, like "python": {...}
        if isinstance(value, dict) and "id" in value and "contract" in value:
            out.append(block)
    return out


@pytest.mark.parametrize("block", _manifests(), ids=lambda b: b.id)
def test_a_manifest_shown_is_a_valid_manifest(block: Block) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    validator = jsonschema.Draft202012Validator(json.loads(SCHEMA.read_text()))
    errors = sorted(validator.iter_errors(json.loads(block.code)), key=str)
    assert not errors, f"{block.id}:\n" + "\n".join(
        f"  {'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}" for e in errors
    )


def test_the_parser_reads_the_markers(tmp_path: Path) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text(
        "```python\nx = 1\n```\n\n<!-- continues -->\n```python\nassert x == 1\n```\n\n"
        "<!-- illustrative -->\n```python\nundefined()\n```\n\n"
        "<!-- file: a/b.py -->\n```python\npass\n```\n\n"
        "1. a step\n\n   ```python\n   y = 2\n   ```\n"
    )
    found = blocks(doc)
    assert [b.marker for b in found] == [None, "continues", "illustrative", "file", None]
    assert found[3].file == "a/b.py"
    assert found[4].code == "y = 2\n"
    namespace: dict = {}
    _run(found[0].code, namespace)
    _run(found[1].code, namespace)
    _run("import asyncio\nawait asyncio.sleep(0)\nz = 3\n", namespace)
    assert namespace["z"] == 3

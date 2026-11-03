"""Every Python block in this package's README runs as it is pasted.

A block that fails here is a block a reader copies and watches fail.
The convention, shared with igni's own documentation tests:

* a ```python block runs on its own, in a fresh namespace, with
  top-level `await` allowed;
* `<!-- continues -->` on the line before the fence runs it in the
  previous block's namespace, after it;
* `<!-- illustrative -->` on the line before the fence only compiles it —
  for a block that needs a live igni server. Use it sparingly.

No block may need the network.
"""

from __future__ import annotations

import ast
import asyncio
import re
import textwrap
import types
from pathlib import Path

import pytest
from pydantic import BaseModel

README = Path(__file__).resolve().parent.parent / "README.md"

_FENCE = re.compile(r"^(?P<indent>[ \t]*)```(?P<lang>[\w-]*)\s*$")


class Block(BaseModel):
    line: int
    code: str
    marker: str = ""


class ReadmeBlocks:
    """The ```python blocks of a Markdown file, dedented, with their markers."""

    @staticmethod
    def read(path: Path) -> list[Block]:
        lines = path.read_text().splitlines()
        blocks: list[Block] = []
        i = 0
        while i < len(lines):
            opened = _FENCE.match(lines[i])
            if opened is None:
                i += 1
                continue
            body: list[str] = []
            j = i + 1
            while j < len(lines) and not re.match(r"^[ \t]*```\s*$", lines[j]):
                body.append(lines[j])
                j += 1
            if opened.group("lang") == "python":
                before = lines[i - 1].strip() if i > 0 else ""
                marker = ""
                for name in ("continues", "illustrative"):
                    if before == f"<!-- {name} -->":
                        marker = name
                blocks.append(
                    Block(line=i + 1, code=textwrap.dedent("\n".join(body)) + "\n", marker=marker)
                )
            i = j + 1
        return blocks


class BlockRunner:
    """Runs a chain of blocks in one namespace and one event loop, top-level `await` and all."""

    @staticmethod
    async def run(chain: list[Block], namespace: dict[str, object], name: str) -> None:
        for block in chain:
            code = compile(
                block.code, f"{name}:{block.line}", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT
            )
            if block.marker == "illustrative":
                continue
            result = types.FunctionType(code, namespace)()
            if asyncio.iscoroutine(result):
                await result


def _chains(path: Path) -> list[list[Block]]:
    chains: list[list[Block]] = []
    for block in ReadmeBlocks.read(path):
        if block.marker == "continues" and chains:
            chains[-1].append(block)
        else:
            chains.append([block])
    return chains


@pytest.mark.parametrize("chain", _chains(README), ids=lambda chain: f"README.md:{chain[0].line}")
def test_the_block_runs(
    chain: list[Block], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    namespace: dict[str, object] = {"__name__": "readme"}
    asyncio.run(BlockRunner.run(chain, namespace, README.name))


def test_the_blocks_are_found() -> None:
    assert len(ReadmeBlocks.read(README)) >= 5

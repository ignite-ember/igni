"""No source file may contain a control byte.

`clients/web/src/components/panels/HooksPanel.tsx` had a literal NUL in
it. Not a corruption — a deliberate separator, joining a hook's fields
into a dedup key with a byte no field can contain, which is the right
choice. It was written as a raw byte rather than the `\\u0000` escape.

The cost was invisible until it mattered. `file` reported the component
as "data"; `grep` classified it as binary; and `grep -rl` therefore
**omitted it from the match list**. So when every other file importing
`IgniClient` was renamed, this one was not — and the only thing that
caught it was `tsc`, which reads files properly and reported an import
of a symbol that no longer existed.

That is the failure this repository keeps finding in other forms: a
guard that silently measures less than it claims. Every rule stated over
"all the source files" — this file's own naming and documentation
checks, a lint sweep, a grep-driven refactor — quietly excludes any file
a tool decides is binary, and says nothing about having done so.

So: no control bytes in source. Tab, newline and carriage return are
allowed; everything below 0x20 otherwise is not. A separator that needs
to be unprintable is written as an escape, where it is visible to a
reader and invisible to nothing.
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Text formats we author. Binary assets are excluded by extension
#: rather than by sniffing, because "is this file binary" is exactly the
#: question that produced the bug.
_SOURCE_SUFFIXES = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".rs",
    ".md",
    ".css",
    ".html",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".sh",
}

#: Tab, newline, carriage return. Everything else below 0x20 is a
#: control character with no business in source.
_ALLOWED = {0x09, 0x0A, 0x0D}


def _source_files() -> list[pathlib.Path]:
    """Every text file this repository commits.

    Asked of git rather than walked with a skip-list. The first version
    walked the tree and excluded `node_modules`, `.venv`, `dist` and so
    on, which is a list that grows by one entry every time somebody adds
    a tool — and it immediately missed two: a downloaded VS Code
    installation under `.vscode-test/`, and hash-named webview bundles
    in the extension clients. Both are full of control bytes and neither
    is anybody's source.

    "Tracked by git" is the property that actually separates the two.
    Every one of those artefacts is either gitignored or simply not
    added, and anything somebody genuinely authored is committed.
    """
    listed = subprocess.run(  # noqa: S603
        ["git", "ls-files", "-z"],
        cwd=_ROOT,
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8", "surrogateescape")

    out = []
    for name in listed.split("\0"):
        if not name:
            continue
        path = _ROOT / name
        if path.suffix in _SOURCE_SUFFIXES and path.is_file():
            out.append(path)
    return out


def test_the_scan_found_the_source_tree():
    """A rule over an empty list passes for the wrong reason — which is
    the same class of mistake this whole file is about."""
    files = _source_files()

    assert len(files) > 500, len(files)
    assert any(f.suffix == ".tsx" for f in files), "no TSX files found; the web client is missing"
    assert any(f.suffix == ".py" for f in files), "no Python files found"
    assert any(f.suffix == ".rs" for f in files), "no Rust files found; the desktop app is missing"


def test_no_source_file_contains_a_control_byte():
    offenders: dict[str, list[str]] = {}
    for path in _source_files():
        data = path.read_bytes()
        found = sorted({hex(b) for b in data if b < 0x20 and b not in _ALLOWED})
        if found:
            offenders[str(path.relative_to(_ROOT))] = found

    assert not offenders, (
        f"these source files contain control bytes: {offenders}. Tools classify them as "
        f"binary and skip them silently — `grep -rl` will omit them from a refactor, and "
        f"any rule stated over 'all source files' quietly stops covering them. Write the "
        f"byte as an escape (`\\u0000`) instead."
    )


@pytest.mark.parametrize(
    "path",
    ["clients/web/src/components/panels/HooksPanel.tsx"],
)
def test_the_file_that_motivated_this_is_readable(path):
    """The specific case, pinned.

    It still needs a NUL as its dedup separator, so a naive "remove the
    NUL" fix would have changed behaviour: two hooks differing only at a
    field boundary would collide on the joined key. The escape keeps the
    byte and loses the problem, and this asserts both halves.
    """
    text = (_ROOT / path).read_text(encoding="utf-8")

    assert "\\u0000" in text, "the escaped separator is gone; has the dedup key changed?"
    assert "\x00" not in text, "the raw NUL is back, and the file is binary again"

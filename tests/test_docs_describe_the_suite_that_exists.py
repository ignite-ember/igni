"""The testing documents must describe *this* suite, not a remembered one.

Sibling to `test_the_docs_name_real_things.py`, which checks that the paths
and config keys a document quotes are real. This one checks the four claims
those rules cannot see, each of which was false when it was written:

1. **Every test file a document names exists.** `docs/TEST_COVERAGE.md` was
   a hand-typed census headed "912 tests across 53 test files". The suite
   was 4844 tests across 299 files, and 12 of the 52 filenames in the table
   had been deleted — including the one in the row about the permission
   system, the first row a reader checks when asking whether the safety
   code is tested. `docs/TESTING.md` had a smaller version of the same
   table with 5 dead names in it.

2. **Every install extra a document advertises is defined.** Four places
   told readers to install a `knowledge` extra for `chromadb`. There is no
   such extra in `pyproject.toml`, and `chromadb` stopped being a
   dependency when the vectors moved to Neo4j. This one is nastier than a
   wrong path because it does not fail: pip *warns* about an extra it
   cannot find and carries on, so the command appears to work and installs
   nothing.

3. **Every pytest marker a document teaches is registered.** `TESTING.md`
   taught `slow` and `requires_api_key`, and told readers to deselect with
   `-m "not slow"`. Neither marker is registered or applied anywhere, so
   that command silently selects the entire suite — and the `--run-slow`
   flag alongside it was not registered either, which is not silent: pytest
   exits with `error: unrecognized arguments` and nothing runs.

4. **The CI section names the interpreter CI actually uses.** `TESTING.md`
   described a 3.10/3.11/3.12/3.13 matrix. `pyproject.toml` pins
   `requires-python = ">=3.12,<3.13"`, `.python-version` holds one version,
   and `ci.yml` carries a comment about why the matrix was deliberately
   removed. Two of the four versions were below the floor and one above the
   ceiling.

**Rules, not lists.** Each is stated over the document corpus and compared
against something derived from the repository — the tree, `pyproject.toml`,
pytest's own marker registry, `.python-version`. A version that checked for
the twelve dead filenames by name would go green the moment a thirteenth
arrived.

**What this cannot do** is tell whether a paragraph is true. A page can
describe the wrong testing strategy entirely while naming only files that
exist.
"""

from __future__ import annotations

import pathlib
import re
import tomllib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_DOCS = sorted((_ROOT / "docs").glob("*.md")) + sorted(_ROOT.glob("*.md"))
_TESTS = _ROOT / "tests"
_TESTING_MD = _ROOT / "docs" / "TESTING.md"

#: A test file cited in backticks — `test_settings.py`, `tests/test_cli.py`.
#: Backticks are what makes it a citation. A bare filename inside a fenced
#: example (`# tests/evals/test_editor.py` at the top of a code sample in
#: `EVALS.md`) is a template for a file the reader is about to write, not a
#: claim that one exists, and a rule that could not tell the difference
#: would have to be ignored.
_TEST_FILE = re.compile(r"`([\w./-]*test_[a-z0-9_]+\.py)[^`]*`")

#: An extras list attached to an install command: `pip install
#: ember-code[mcp]`, `pip install -e ".[dev,mcp,web]"`, `uv sync --extra
#: dev`. Anchoring on the command matters — a bare ``[...]`` also matches
#: markdown links, YAML flow lists and type parameters, and the unanchored
#: version of this pattern reported 18 "extras" in one document.
_INSTALL_EXTRAS = re.compile(
    r"(?:pip install|uv add|uv pip install)[^\n`]*?[\"'.]?(?:ember[-_]code|\.)"
    r"\[([a-z0-9_]+(?:\s*,\s*[a-z0-9_]+)*)\]"
)
_EXTRA_FLAG = re.compile(r"--extra[= ]([a-z0-9_-]+)")

_MARK_DECORATOR = re.compile(r"@pytest\.mark\.([a-z_][a-z0-9_]*)")
#: ``-m "not slow"``. Quoted, and only on a line that is about pytest —
#: the two other things that look like this are ``igni -m "your prompt"``
#: (the CLI's message flag, on lines that never mention pytest) and
#: ``python -m pytest`` (a module name, unquoted).
_MARK_SELECTOR = re.compile(r'-m\s+"(?:not\s+)?([a-z_][a-z0-9_]*)"')
#: A custom flag, which pytest cannot know about unless this repository
#: registers it. ``--cov`` / ``--collect-only`` come from pytest and its
#: plugins; ``--run-something`` is ours or it does not exist.
_RUN_FLAG = re.compile(r"(--run-[a-z0-9-]+)")
_ADDOPTION = re.compile(r"""addoption\(\s*["'](--[a-z0-9-]+)["']""")

#: Documents whose subject is the past. A migration record or a dated
#: verification plan naming a test file that has since been deleted is the
#: record of what was run at the time; editing the filename out would leave
#: an entry about nothing. Same reasoning, and three of the same four
#: files, as ``_HISTORICAL`` in ``test_the_docs_name_real_things.py``.
_RECORDS = {
    "NEO4J_MIGRATION_DONE.md",
    "REFACTOR_PROGRESS.md",
    "CLAUDE_CODE_PARITY.md",
    # A dated parity matrix — "178/178 automated tests passing + live
    # walkthrough through the Tauri app on 2026-06-29". The filenames in it
    # are what that walkthrough covered on that date.
    "TESTING_PLAN.md",
}

#: Known offenders nobody has fixed yet, kept explicit so the rule can stay
#: stated over the whole corpus instead of quietly narrowing its glob to the
#: files that happen to pass. ``QUICKSTART.md`` still advertises the dead
#: ``knowledge`` extra, and so does ``Makefile``'s ``install`` target (which
#: is not markdown, so this rule cannot see it at all — fix both together).
#: ``test_the_exemption_is_still_needed`` below fails once it is fixed, so
#: this cannot outlive the bug.
_EXTRAS_TODO = {"QUICKSTART.md"}


def _defined_extras() -> set[str]:
    data = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    return set(data["project"].get("optional-dependencies", {}))


def _cited_test_files(doc: pathlib.Path) -> set[str]:
    return set(_TEST_FILE.findall(doc.read_text()))


def _advertised_extras(doc: pathlib.Path) -> set[str]:
    text = doc.read_text()
    named = {n.strip() for m in _INSTALL_EXTRAS.finditer(text) for n in m.group(1).split(",")}
    return named | set(_EXTRA_FLAG.findall(text))


def _taught_markers(doc: pathlib.Path) -> set[str]:
    text = doc.read_text()
    found = set(_MARK_DECORATOR.findall(text))
    for line in text.splitlines():
        if "pytest" in line:
            found |= set(_MARK_SELECTOR.findall(line))
    return found


def _registered_addoptions() -> set[str]:
    return {
        flag
        for conftest in _TESTS.rglob("conftest.py")
        for flag in _ADDOPTION.findall(conftest.read_text())
    }


def _ci_section() -> str:
    """The `## CI Integration` heading through to the next `##` or EOF."""
    text = _TESTING_MD.read_text()
    start = text.index("## CI Integration")
    rest = text[start + len("## CI Integration") :]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


class TestTheRulesMeasureSomething:
    """Every rule compares two sets derived from the repository. An empty
    set on either side passes everything, which is how a rule like this
    dies without anyone noticing."""

    def test_there_are_documents_to_check(self):
        assert len(_DOCS) > 20, [d.name for d in _DOCS]

    def test_the_tree_has_tests_in_it(self):
        assert len(list(_TESTS.rglob("test_*.py"))) > 200

    def test_the_extras_are_readable(self):
        extras = _defined_extras()

        assert extras, "no optional-dependencies parsed; rule 2 measures nothing"
        assert "dev" in extras, sorted(extras)
        assert "knowledge" not in extras, (
            "a `knowledge` extra now exists, so the documents that advertise it are "
            "right and this rule's premise is stale — recheck the whole file"
        )

    def test_the_citation_pattern_finds_citations(self):
        """Across the corpus: a threshold pinned to one document fails when
        that document legitimately stops citing files, which says nothing
        about the pattern."""
        found = {n for doc in _DOCS for n in _cited_test_files(doc)}

        assert len(found) > 20, sorted(found)

    def test_the_extras_pattern_tells_an_install_from_a_link(self):
        assert _advertised_extras(_ROOT / "docs" / "DEVELOPMENT.md"), (
            "DEVELOPMENT.md documents an install command with extras; if this "
            "finds none, the pattern stopped matching and rule 2 is vacuous"
        )
        assert not _INSTALL_EXTRAS.search("see [the guide](x.md) and `list[str]`")

    def test_the_marker_pattern_tells_a_selector_from_the_other_dash_ms(self):
        """`igni -m "your prompt"` is the CLI's message flag and `python -m
        pytest` is a module name. A rule that read either as a marker
        selector would cry wolf, and a rule that cries wolf gets deleted by
        the next person."""
        assert not _taught_markers(_ROOT / "docs" / "MIGRATION.md")
        assert not _MARK_SELECTOR.search("uv run python -m pytest tests/")
        assert _MARK_SELECTOR.findall('uv run pytest -m "not integration"') == ["integration"]

    def test_the_ci_section_is_findable(self):
        assert len(_ci_section().strip()) > 50


@pytest.mark.parametrize("doc", _DOCS, ids=lambda d: d.name)
def test_every_test_file_it_names_exists(doc):
    if doc.name in _RECORDS:
        pytest.skip(f"{doc.name} records what was true at the time; see _RECORDS")

    present = {p.name for p in _TESTS.rglob("test_*.py")}
    missing = sorted(n for n in _cited_test_files(doc) if pathlib.Path(n).name not in present)

    assert not missing, (
        f"{doc.name} names test files that are not in the repository: {missing}. A "
        f"reader sent to a deleted file concludes the behaviour is untested, or that "
        f"they cannot find it — both worse than the page saying nothing."
    )


@pytest.mark.parametrize("doc", _DOCS, ids=lambda d: d.name)
def test_every_install_extra_it_advertises_is_defined(doc):
    """The rule with a user on the other end of it.

    pip only warns about an extra that does not exist. The install appears
    to succeed, the optional dependency is absent, and the failure surfaces
    later as a missing import with nothing to connect it back.
    """
    if doc.name in _EXTRAS_TODO:
        pytest.skip(f"{doc.name} is a known offender outside this change; see _EXTRAS_TODO")

    unknown = sorted(_advertised_extras(doc) - _defined_extras())

    assert not unknown, (
        f"{doc.name} tells the reader to install extras that `pyproject.toml` does not "
        f"define: {unknown}. pip warns and continues, so the command looks like it "
        f"worked."
    )


@pytest.mark.parametrize("doc", _DOCS, ids=lambda d: d.name)
def test_every_pytest_marker_it_teaches_is_registered(doc, request):
    """A marker nobody registered is a selector that matches nothing.

    `-m "not slow"` against an unregistered `slow` deselects zero tests and
    reports success, so the reader believes they ran the fast subset and in
    fact ran everything — the failure mode where the documentation makes
    you *more* wrong than no documentation.
    """
    if doc.name in _RECORDS:
        pytest.skip(f"{doc.name} records what was true at the time; see _RECORDS")

    registered = {line.split(":")[0].split("(")[0] for line in request.config.getini("markers")}
    unknown = sorted(_taught_markers(doc) - registered)

    assert not unknown, (
        f"{doc.name} teaches pytest markers that are not registered: {unknown}. "
        f"Register them in `tests/conftest.py`'s `pytest_configure`, or stop "
        f"documenting them. Registered: {sorted(registered)}"
    )


@pytest.mark.parametrize("doc", _DOCS, ids=lambda d: d.name)
def test_every_custom_pytest_flag_it_teaches_is_registered(doc):
    """`--run-*` flags are this repository's own; pytest rejects any it was
    not told about, so a documented one that no `pytest_addoption` creates
    is not a silent no-op but a run that never starts."""
    if doc.name in _RECORDS:
        pytest.skip(f"{doc.name} records what was true at the time; see _RECORDS")

    unknown = sorted(set(_RUN_FLAG.findall(doc.read_text())) - _registered_addoptions())

    assert not unknown, (
        f"{doc.name} documents pytest flags that no `pytest_addoption` registers: "
        f"{unknown}. pytest exits with `error: unrecognized arguments` and collects "
        f"nothing."
    )


class TestTheCiSectionMatchesCi:
    """`docs/TESTING.md` is where a contributor looks to find out what has
    to be green. Describing a matrix that `ci.yml` deliberately does not
    have sends them to reproduce a failure on interpreters the product
    refuses to install."""

    def test_it_names_the_interpreter_ci_pins_and_no_other(self):
        pinned = (_ROOT / ".python-version").read_text().strip()
        series = ".".join(pinned.split(".")[:2])
        named = set(re.findall(r"\b3\.\d+\b", _ci_section()))

        assert named == {series}, (
            f"the CI Integration section names Python {sorted(named)}; `.python-version` "
            f"says {pinned} and `pyproject.toml` pins `requires-python` to that one "
            f"version. Anything else is a version the product never ships on."
        )

    def test_the_pytest_command_it_quotes_is_the_one_ci_runs(self):
        ci = (_ROOT / ".github" / "workflows" / "ci.yml").read_text()
        quoted = [
            c
            for c in re.findall(r"`([^`\n]*pytest[^`\n]*)`", _ci_section())
            if c.startswith("uv run pytest")
        ]

        assert quoted, "the CI section quotes no pytest command, so this checks nothing"
        for command in quoted:
            assert command in ci, (
                f"the CI Integration section says CI runs `{command}`; no such line is in "
                f"`.github/workflows/ci.yml`. The flags matter — `-rs` is what puts the "
                f"skip reasons in the log, which is how you tell an absent Neo4j from a "
                f"suite that collected nothing."
            )


class TestTheExemptionsAreNarrow:
    """An exemption is a hole, so it has to keep earning its cost."""

    def test_every_exempt_document_exists(self):
        missing = sorted(
            n
            for n in _RECORDS | _EXTRAS_TODO
            if not (_ROOT / "docs" / n).exists() and not (_ROOT / n).exists()
        )

        assert not missing, f"{missing} are exempt and not in the repository"

    def test_the_exemption_is_still_needed(self):
        """If an exempt document stops offending, the hole should close
        rather than sit open waiting to hide the next one."""
        present = {p.name for p in _TESTS.rglob("test_*.py")}
        clean = sorted(
            name
            for name in _RECORDS
            for doc in [
                (_ROOT / "docs" / name) if (_ROOT / "docs" / name).exists() else _ROOT / name
            ]
            if all(pathlib.Path(n).name in present for n in _cited_test_files(doc))
        )

        assert not clean, (
            f"{clean} name only test files that exist and no longer need the _RECORDS "
            f"exemption — take them off the list"
        )

    def test_the_extras_todo_is_still_a_todo(self):
        offenders = sorted(
            name
            for name in _EXTRAS_TODO
            for doc in [
                (_ROOT / "docs" / name) if (_ROOT / "docs" / name).exists() else _ROOT / name
            ]
            if _advertised_extras(doc) - _defined_extras()
        )

        assert offenders == sorted(_EXTRAS_TODO), (
            f"these are exempt from the extras rule and no longer need to be: "
            f"{sorted(set(_EXTRAS_TODO) - set(offenders))}. Delete the entry — and "
            f"check `Makefile`'s `install` target at the same time, which this rule "
            f"cannot see."
        )

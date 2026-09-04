"""Every path, config key and product name in the docs must be real.

Ported from the igni server, where `docs/ARCHITECTURE.md` turned out to
describe a storage layer that exists nowhere in the repository — a
VectorBridge/Weaviate stack, a pipeline that had moved to another
service, and eight environment variables the application does not read.
None of it was a mistake. Each became false through a correct change
somewhere else, with nothing connecting the two.

This repository had the same disease, and one strain the server did not:

* **`docs/CONFIGURATION.md` documented an `embeddings:` config block.**
  There is no such field on `Settings`, and `Settings` is a plain
  `BaseModel` — unknown keys are dropped in silence. So a customer who
  followed the documentation got a config file that did nothing, with
  no error to tell them. `docs/ONBOARDING.md` did the same with an
  `onboarding:` block.
* **the same document introduced its first example as "Built-in
  registry (hardcoded in defaults.py)".** There is no `defaults.py`,
  and the built-in registry is empty — models arrive from the
  customer's own server after sign-in.
* **six documents cited paths that do not exist**, mostly modules that
  moved when the tools package was reorganised.
* **192 places called the product "Ember".** It is called igni.

Three rules, all stated over every markdown file rather than over a list
of the ones somebody has audited — the version that would have passed
while `CONFIGURATION.md` rotted:

1. every repository path quoted in backticks exists;
2. every top-level key in a fence *labelled as the igni config file* is
   a real field on `Settings`;
3. no document calls the product Ember.

Rule 2 is deliberately narrow. Documents show YAML for agent
frontmatter, eval cases, sync manifests and permission blocks, none of
which are `Settings` — a rule that flagged those would cry wolf, and a
rule that cries wolf gets deleted by the next person. The label
convention (`# .igni/config.yaml` as the fence's first line) already
existed in `CONFIGURATION.md`; this makes it load-bearing.

**What none of this does** is tell whether a paragraph is true. A
document can describe the wrong architecture entirely in prose that
quotes only real paths. These rules remove a class of rot; they do not
make a document trustworthy.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from ember_code.core.config.settings import Settings

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_DOCS = sorted((_ROOT / "docs").glob("*.md")) + sorted(_ROOT.glob("*.md"))

_REPO_DIRS = ("src", "tests", "clients", "tools", "docs", "scripts")
_PATH = re.compile(rf'`((?:{"|".join(_REPO_DIRS)})/[\w./-]*[\w/])`')

#: A fence that claims to be the igni config file. The convention was
#: already in `CONFIGURATION.md`; this is what makes it mean something.
_CONFIG_FENCE = re.compile(
    r"```ya?ml\n(#\s*(?:~/)?\.igni/config\.yaml[^\n]*\n.*?)```", re.S
)
_TOP_LEVEL_KEY = re.compile(r"^([a-z][a-z0-9_]*):", re.M)

#: The product is igni. What is allowed to contain "ember":
#:
#: * `ignite-ember` and `Ignite Ember` — the GitHub org and the company;
#: * `ember-code` / `ember-server` / `ember-iac` — sibling repository
#:   directory names, and `ember_code` the Python package;
#: * `.ember` — the previous configuration directory, still read when
#:   the current one is absent, so a document explaining that has to be
#:   able to name it.
#:
#: A bare "ember" anywhere else is not.
_LEGITIMATE = re.compile(
    r"ember[-_](?:code|server|iac)|ignite-ember|Ignite Ember|ember_code|\.ember\b"
)
_EMBER = re.compile(r"\bEmber\b|\bember\b")

#: Documents whose subject is the past. A migration record naming a test
#: file that has since been deleted is the record of what was migrated,
#: and editing the path out would leave a note about nothing.
_HISTORICAL = {
    "NEO4J_MIGRATION_DONE.md",
    "REFACTOR_PROGRESS.md",
    "MIGRATION.md",
    # `CONFIG_LAYOUT_PLAN.md` was on this list and came off it: every
    # "ember" in it turned out to be `.ember`, the previous config
    # directory, which a document about renaming it has to be able to
    # name. `test_the_exemption_is_still_earning_its_keep` is what
    # found that — an exemption protecting nothing is a hole waiting to
    # hide the next document.
    # A dated parity log — "2026-06-28: TUI formatting helpers covered,
    # `tests/test_tui_formatting.py` (23 cases)". Those tests existed on
    # that date and have since been merged or renamed. The entry is the
    # record of what was covered when; editing the filename out would
    # leave a log entry about nothing.
    "CLAUDE_CODE_PARITY.md",
}


def _find(name: str) -> pathlib.Path:
    """An exempt document, wherever it lives.

    Some are in `docs/` and some at the repository root, and a lookup
    that only checked one silently treated the other as absent.
    """
    for candidate in (_ROOT / "docs" / name, _ROOT / name):
        if candidate.exists():
            return candidate
    return _ROOT / "docs" / name


def _settings_fields() -> set[str]:
    return set(Settings.model_fields)


class TestTheRulesMeasureSomething:
    """Every rule compares against a set derived from the repository.
    An empty set on either side passes everything."""

    def test_there_are_documents_to_check(self):
        assert len(_DOCS) > 20, [d.name for d in _DOCS]

    def test_the_settings_are_readable(self):
        fields = _settings_fields()

        assert len(fields) > 15, sorted(fields)
        assert "models" in fields and "permissions" in fields

    def test_the_path_pattern_finds_paths(self):
        """Across the corpus, not in one file: a threshold pinned to a
        single document fails when that document legitimately stops
        quoting paths, which says nothing about the pattern."""
        found = {p for doc in _DOCS for p in _PATH.findall(doc.read_text())}

        assert len(found) > 30, sorted(found)
        assert "src/ember_code/core/config/settings.py" in found or any(
            f.startswith("src/ember_code/") for f in found
        ), sorted(found)[:20]

    def test_the_config_fence_pattern_finds_fences(self):
        fences = _CONFIG_FENCE.findall((_ROOT / "docs" / "CONFIGURATION.md").read_text())

        assert fences, "no labelled config fences found, so rule 2 measures nothing"
        assert any("models:" in f for f in fences), fences

    def test_the_naming_pattern_tells_a_product_from_a_path(self):
        """`ember-code` is a directory. A rule that could not tell the
        difference would have to be ignored, which is the same as not
        having one."""
        assert _EMBER.search(_LEGITIMATE.sub("", "the Ember Cloud gateway"))
        assert not _EMBER.search(_LEGITIMATE.sub("", "`ember-code/src`"))
        assert not _EMBER.search(_LEGITIMATE.sub("", "https://api.ignite-ember.sh"))
        assert not _EMBER.search(_LEGITIMATE.sub("", "from ember_code.core import x"))


@pytest.mark.parametrize("doc", _DOCS, ids=lambda d: d.name)
def test_every_repository_path_it_quotes_exists(doc):
    if doc.name in _HISTORICAL:
        pytest.skip(f"{doc.name} records what was true at the time; see _HISTORICAL")

    quoted = _PATH.findall(doc.read_text())
    concrete = [p for p in quoted if "*" not in p and "{" not in p and "..." not in p]
    missing = sorted({p for p in concrete if not (_ROOT / p).exists()})

    assert not missing, (
        f"{doc.name} quotes paths that do not exist: {missing}. Either the document "
        f"describes a version of this repository that is gone, or the file moved and "
        f"nothing followed it."
    )


@pytest.mark.parametrize("doc", _DOCS, ids=lambda d: d.name)
def test_every_documented_config_key_is_real(doc):
    """The rule with a customer on the other end of it.

    `Settings` is a plain `BaseModel`, so an unknown key is dropped
    without a word. A reader who copies a documented block into
    `~/.igni/config.yaml` gets a file that does nothing and no way to
    find out why — which is worse than an error, and worse than no
    documentation.
    """
    known = _settings_fields()
    documented: set[str] = set()
    for fence in _CONFIG_FENCE.findall(doc.read_text()):
        documented |= set(_TOP_LEVEL_KEY.findall(fence))

    unknown = sorted(documented - known)

    assert not unknown, (
        f"{doc.name} documents top-level config keys that `Settings` does not have: "
        f"{unknown}. Unknown keys are dropped in silence, so a reader who copies this "
        f"gets a config file that does nothing."
    )


@pytest.mark.parametrize("doc", _DOCS, ids=lambda d: d.name)
def test_no_document_calls_the_product_ember(doc):
    """It is called igni.

    `ignite-ember` (the GitHub org, the release host) and the directory
    and module names are excluded above, so what this catches is prose:
    "Ember Cloud", "the Ember embeddings API", "Ember uses DDG".
    """
    if doc.name in _HISTORICAL:
        pytest.skip(f"{doc.name} records what was true at the time; see _HISTORICAL")

    offenders = [
        line.strip()
        for line in doc.read_text().splitlines()
        if _EMBER.search(_LEGITIMATE.sub("", line))
    ]

    assert not offenders, (
        f"{doc.name} calls the product Ember in {len(offenders)} place(s). It is called "
        f"igni. First: {offenders[0][:120]!r}"
    )


class TestTheHistoricalExemptionIsNarrow:
    """An exemption is a hole, so it has to earn its cost."""

    def test_every_exempt_document_exists(self):
        """An exemption naming a file that has been deleted protects
        nothing and hides the next one."""
        missing = sorted(n for n in _HISTORICAL if not _find(n).exists())

        assert not missing, f"{missing} are exempt and not there"

    def test_each_exempt_document_is_actually_a_record_of_the_past(self):
        for name in sorted(_HISTORICAL):
            prose = _find(name).read_text().lower()

            assert any(
                word in prose[:3000]
                for word in (
                    "migration",
                    "rename",
                    "was ",
                    "previously",
                    "progress",
                    "generated 2026",
                    "comparison",
                )
            ), f"{name} is exempt but does not read as a record of a past change"

    def test_the_exemption_is_still_earning_its_keep(self):
        """If none of them names anything gone, the hole should close
        rather than sit open waiting to hide the next document."""
        still_needed = []
        for name in sorted(_HISTORICAL):
            doc = _find(name)
            quoted = [
                p
                for p in _PATH.findall(doc.read_text())
                if "*" not in p and "{" not in p and "..." not in p
            ]
            if any(not (_ROOT / p).exists() for p in quoted) or any(
                _EMBER.search(_LEGITIMATE.sub("", line))
                for line in doc.read_text().splitlines()
            ):
                still_needed.append(name)

        assert still_needed == sorted(_HISTORICAL), (
            f"these are exempt and no longer need to be: "
            f"{sorted(set(_HISTORICAL) - set(still_needed))}"
        )

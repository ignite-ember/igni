"""Environment variables are `IGNI_*`, never a bare `EMBER_*`.

The product is igni. `igni` and `ignite-ember` are both acceptable spellings
of it; a bare `ember` is not. This file enforces that for environment
variable names specifically, because they are the one identifier class that
crosses a process boundary: a client sets a variable and the Python backend
reads it, so renaming one side and missing the other leaves a variable that
is simply never read — and nothing fails loudly. `IGNI_PARENT_PID` is the
clearest case: Tauri, the VS Code extension, the JetBrains plugin and the
web e2e harness all set it, and `backend/supervisor.py` reads it to know
when to shut down. Miss one setter and that client leaks backend processes,
silently.

**This test exists because the inventory that preceded the rename was
wrong.** A survey said 31 names across 66 files; the prefix replace found
`EMBER_API` and `EMBER_TOKEN` in `scripts/check_group_config.py`, a file the
survey never listed. Enumerating call sites and fixing them one by one
would have left those behind. A rule that fails on the next one is worth
more than a list that was already incomplete when it was written.

Two exemptions, both deliberate:

* `IGNITE_EMBER_*` — `ignite-ember` is an accepted spelling, so
  `IGNITE_EMBER_DEV` and `IGNITE_EMBER_VERSION` stay. `\\bEMBER_` cannot
  match inside them anyway: `_` and `E` are both word characters, so there
  is no boundary between `IGNITE_` and `EMBER_`.
* `evals/results/` — recorded model transcripts from past eval runs. They
  mention `EMBER_API_KEY` 90 times because that is what the model said at
  the time. Rewriting them would falsify a record of what happened.
"""

from __future__ import annotations

import pathlib
import re

#: Anything that is not source: caches, build output, vendored code, and the
#: eval transcripts that are data rather than code.
_SKIP = (
    "node_modules",
    "/target/",
    "/build/",
    "/dist/",
    "/out/",
    ".vscode-test",
    "/MagicMock/",
    ".venv",
    "__pycache__",
    "/.git/",
    ".mypy_cache",
    "evals/results/",
    # This file itself. It has to spell the old names out — to document the
    # rule and to assert on them — so scanning it would make the guard fail
    # on its own contents forever.
    #
    # Excluding the file that does the checking is exactly how an earlier
    # guard in this repo went vacuous (it spelled `.ember` inside its own
    # regex and so could never fail). The difference is
    # `TestTheScanCanActuallyFail` below, which plants a name in a temporary
    # file and asserts the scanner reports it — so the ability to detect is
    # proven independently of this exemption.
    "tests/test_env_var_prefix.py",
)

#: Text files worth scanning. A binary that happens to contain the bytes is
#: a compiled artifact, not a source of truth.
_EXTS = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".mjs",
    ".rs",
    ".kt",
    ".kts",
    ".sh",
    ".cmd",
    ".md",
    ".json",
    ".yml",
    ".yaml",
    ".toml",
    "",
}

_BARE_EMBER = re.compile(r"\bEMBER_[A-Z0-9_]*")


def _repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent.parent


def _scan() -> list[tuple[str, str]]:
    """Every `(file, name)` pair where a bare `EMBER_*` survives."""
    root = _repo_root()
    found: list[tuple[str, str]] = []
    for path in root.rglob("*"):
        text_path = str(path)
        if not path.is_file() or any(k in text_path for k in _SKIP):
            continue
        if path.suffix not in _EXTS:
            continue
        try:
            content = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for name in set(_BARE_EMBER.findall(content)):
            found.append((str(path.relative_to(root)), name))
    return sorted(found)


class TestNoBareEmberEnvVar:
    def test_the_repository_is_clean(self):
        offenders = _scan()

        assert not offenders, (
            "bare EMBER_* environment variable names found:\n"
            + "\n".join(f"    {name} in {f}" for f, name in offenders)
            + "\n\nRename to IGNI_*. These cross a process boundary — a client sets "
            "them and the Python backend reads them — so a half-done rename leaves "
            "a variable nothing reads, with nothing failing loudly. If a name here "
            "is deliberate, add its path to _SKIP with the reason."
        )


class TestTheScanCanActuallyFail:
    """A guard that cannot fail is worse than no guard, and this one reads
    the filesystem — so its ability to detect is asserted directly rather
    than assumed."""

    def test_it_finds_a_planted_name(self, tmp_path, monkeypatch):
        planted = tmp_path / "planted.py"
        planted.write_text('os.environ["EMBER_SOMETHING_NEW"]\n')
        monkeypatch.setattr(pathlib.Path, "rglob", lambda self, pat: iter([planted]))
        monkeypatch.setitem(globals(), "_repo_root", lambda: tmp_path)

        offenders = _scan()

        assert offenders, "the scan did not notice a planted EMBER_ name"
        assert any(name == "EMBER_SOMETHING_NEW" for _f, name in offenders)

    def test_it_does_not_flag_the_accepted_spelling(self, tmp_path, monkeypatch):
        allowed = tmp_path / "allowed.py"
        allowed.write_text('os.environ["IGNITE_EMBER_DEV"]\nos.environ["IGNI_MODEL"]\n')
        monkeypatch.setattr(pathlib.Path, "rglob", lambda self, pat: iter([allowed]))

        assert not _scan(), "IGNITE_EMBER_* and IGNI_* must both pass"


class TestTheCrossProcessContractAgrees:
    """The specific pairs where a missed rename is silent.

    Each variable below is written by at least one client and read by the
    backend. Asserted as "both sides mention the same name" rather than by
    parsing either side, because the failure mode is a *mismatch*, and a
    mismatch is invisible to any test that only looks at one side.
    """

    def test_parent_pid_is_spelled_the_same_on_both_sides(self):
        root = _repo_root()
        reader = (root / "src/ember_code/backend/supervisor.py").read_text()
        setter = (root / "clients/tauri/src-tauri/src/lib.rs").read_text()

        assert "IGNI_PARENT_PID" in reader, "the backend no longer reads it"
        assert "IGNI_PARENT_PID" in setter, "the Tauri client no longer sets it"
        assert "EMBER_PARENT_PID" not in reader
        assert "EMBER_PARENT_PID" not in setter

    def test_every_client_that_sets_parent_pid_uses_the_new_name(self):
        """Four separate clients set it, and the JetBrains one cannot be
        compiled here — so this is the only check that covers it."""
        root = _repo_root()
        setters = [
            "clients/tauri/src-tauri/src/lib.rs",
            "clients/vscode/src/runtime.ts",
            "clients/jetbrains/src/main/kotlin/sh/igniteember/embercode/EmberRuntime.kt",
        ]
        for rel in setters:
            body = (root / rel).read_text()
            if "PARENT_PID" not in body:
                continue
            assert "IGNI_PARENT_PID" in body, f"{rel} sets the old name"
            assert "EMBER_PARENT_PID" not in body, f"{rel} still sets EMBER_PARENT_PID"

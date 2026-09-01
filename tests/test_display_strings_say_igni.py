"""Text a user reads says `igni`, never a bare `Ember`.

`igni` and `ignite-ember` are both acceptable spellings of the product; a
bare `ember` is not. `test_env_var_prefix.py` enforces that for
environment variables. This file does it for the other class that reaches
people: **display strings in the client shells.**

It exists because the one-by-one fix did not hold. F49 in the production
review found "Ember" in both of the VS Code extension's right-click menu
entries and fixed those two. Four months later the same sweep found seven
more, in places nobody had thought to look:

    clients/web/src/components/panels/LoginPanel.tsx   "Log in to Ember Cloud"   ← a dialog title
    clients/web/src/components/Composer.tsx            "/login → Log in to Ember Cloud"
    clients/web/src/components/Composer.tsx            "/sync-knowledge → Push project knowledge to Ember Cloud"
    clients/web/src/protocol/client.ts                 "[Ember] WS URL:"
    clients/vscode/src/extension.ts                    "Preparing Ember backend…"
    clients/vscode/src/extension.ts                    "Starting Ember backend…"
    clients/vscode/src/extension.ts                    "Ember backend did not become ready within 120s"

`test_env_var_prefix.py`'s own docstring makes the argument for why this
should have been a rule the first time: "a rule that fails on the next one
is worth more than a list that was already incomplete when it was
written."

**Scope, stated precisely.** This checks string literals and JSX text in
the client trees. It does *not* check Python docstrings and comments,
where "Ember Cloud" still appears about twenty times — those are internal
prose, not something a user sees, and rewriting them is a separate
cleanup. It also does not check identifiers: `EmberClient`,
`EmberEditTools`, `ember_code` and friends are code, and renaming them is
a refactor with no user-visible effect.

Worth noting beyond the name: "Ember Cloud" described a hosted service,
and the product decision is self-hosted only. The login those strings
belong to authenticates against the customer's *own* server at `api_url`,
so "Log in to your igni server" is not just the right name, it is the
right sentence.
"""

from __future__ import annotations

import pathlib
import re

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Directories whose strings a user can see.
_CLIENT_TREES = [
    ("clients/web/src", (".ts", ".tsx")),
    ("clients/vscode/src", (".ts",)),
    ("clients/jetbrains/src", (".kt",)),
]

# Identifier tokens that legitimately contain "Ember". Matched as the whole
# identifier-ish run around the word, so `Ember Cloud` yields the bare token
# `Ember` and is not exempted by `EmberClient` being on this list.
_ALLOWED_TOKENS = frozenset(
    {
        "ignite-ember",
        "ignite_ember",
        "IgniteEmber",
        "ember-code",
        "ember_code",
        "ember-server",
        "EmberClient",
        "EmberSpec",
        "EmberEditTools",
        "EmberShellTools",
        "EmberRuntime",
        "EmberBackendService",
        "EmberToolCall",
        # Real build artifacts named in a developer-facing `error()`: the
        # generated resource file and the gradle task that writes it. Renaming
        # those to satisfy a rule about display text would be the tail wagging
        # the dog, and the message has to name them accurately to be useful.
        "ember-version",
        "generateEmberVersion",
    }
)

_TOKEN_AROUND = re.compile(r"[A-Za-z0-9_-]*Ember[A-Za-z0-9_-]*")

_LINE_COMMENT = re.compile(r"//.*$", re.M)
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
# Double- and single-quoted literals, plus template literals. Deliberately
# simple: over-matching here means a stricter test, not a wrong one.
_STRINGS = re.compile(r'"(?:[^"\\\n]|\\.)*"' r"|'(?:[^'\\\n]|\\.)*'" r"|`(?:[^`\\]|\\.)*`", re.S)
# JSX text between tags, e.g. `<div>Log in to Ember Cloud</div>`.
_JSX_TEXT = re.compile(r">([^<>{}\n]*[A-Za-z][^<>{}\n]*)<")


def _user_facing_spans(source: str) -> list[str]:
    """String literals and JSX text, with comments removed first."""
    stripped = _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", source))
    return _STRINGS.findall(stripped) + _JSX_TEXT.findall(stripped)


def _offenders(source: str) -> list[str]:
    found = []
    for span in _user_facing_spans(source):
        for token in _TOKEN_AROUND.findall(span):
            if token not in _ALLOWED_TOKENS:
                found.append(span.strip())
                break
    return found


def _client_files() -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for tree, suffixes in _CLIENT_TREES:
        base = _ROOT / tree
        if not base.exists():
            continue
        out.extend(p for p in base.rglob("*") if p.suffix in suffixes)
    return sorted(out)


def test_the_client_trees_exist():
    """A guard that silently scans nothing is worse than no guard — this is
    the shape of mistake that made two leak sweeps in the production review
    look like successes."""
    files = _client_files()

    assert len(files) > 50, (
        f"only found {len(files)} client source files; the paths must have moved"
    )


@pytest.mark.parametrize("path", _client_files(), ids=lambda p: str(p.relative_to(_ROOT)))
def test_no_bare_ember_in_a_display_string(path: pathlib.Path):
    offenders = _offenders(path.read_text(encoding="utf-8", errors="replace"))

    assert not offenders, (
        f'{path.relative_to(_ROOT)} has user-visible text saying "Ember".\n'
        f"  {chr(10).join('  ' + o for o in offenders)}\n"
        '  Use "igni" (or "ignite-ember"). If this is an identifier rather '
        "than display text, add the exact token to _ALLOWED_TOKENS."
    )


def test_the_rule_catches_what_it_is_meant_to():
    """The extraction is regex-based, so it is worth proving it sees the
    real cases rather than trusting that it does."""
    assert _offenders('<div className="dialog-title">Log in to Ember Cloud</div>')
    assert _offenders('progress("Preparing Ember backend…");')
    assert _offenders('console.info("[Ember] WS URL:", url);')
    assert _offenders("const s = `Ember is replying…`;")


def test_the_rule_does_not_flag_legitimate_uses():
    assert not _offenders('import type { EmberClient } from "./client";')
    assert not _offenders('progress("Preparing the igni backend…");')
    assert not _offenders('const pkg = "ignite-ember";')
    assert not _offenders('spawn("uv", ["tool", "install", "ignite-ember"]);')
    # Comments are internal prose and out of scope.
    assert not _offenders("// the Ember Cloud gateway fallback")
    assert not _offenders("/* Pick a theme matching the current Ember theme */")


# ── Documented commands must exist ───────────────────────────────────────
#
# Separate concern from the product name, same root cause: instructions
# nobody runs. `/login` is a slash command handled *inside* a session, and
# five documents told users to run it at a shell prompt:
#
#     README.md            ignite-ember /login   # sign up for hosted models
#     QUICKSTART.md        ignite-ember /login
#     docs/CONFIGURATION.md
#     docs/MIGRATION.md    (twice)
#
# Verified against the installed wheel: `ignite-ember /login` answers
# `Error: No such command '/login'`. The Quick Start's second line failed.
#
# Scoped to fenced shell blocks on purpose. Those are what people copy;
# prose that *describes* the mistake — including the sentences the fix
# added — must not trip the rule.

_DOC_ROOTS = ["README.md", "QUICKSTART.md", "docs"]
_FENCED_SHELL = re.compile(r"```(?:bash|sh|shell|console|zsh)\n(.*?)```", re.S)
_SHELL_SLASH_COMMAND = re.compile(r"^\s*\$?\s*(?:igni|ignite-ember)\s+/[a-z][\w-]*", re.M)


def _doc_files() -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for entry in _DOC_ROOTS:
        path = _ROOT / entry
        if path.is_dir():
            out.extend(sorted(path.glob("*.md")))
        elif path.exists():
            out.append(path)
    return out


def test_docs_were_found():
    assert len(_doc_files()) > 3, [str(p) for p in _doc_files()]


@pytest.mark.parametrize("path", _doc_files(), ids=lambda p: str(p.relative_to(_ROOT)))
def test_no_shell_block_invokes_a_slash_command(path: pathlib.Path):
    offenders = [
        line.strip()
        for block in _FENCED_SHELL.findall(path.read_text(encoding="utf-8", errors="replace"))
        for line in _SHELL_SLASH_COMMAND.findall(block)
    ]

    assert not offenders, (
        f"{path.relative_to(_ROOT)} tells the reader to run a slash command at a "
        f"shell prompt: {offenders}. Slash commands are handled inside a session; "
        'the CLI answers "No such command".'
    )


def test_that_rule_catches_the_real_case():
    block = "```bash\nignite-ember /login\n```"
    assert _SHELL_SLASH_COMMAND.findall(_FENCED_SHELL.findall(block)[0])


def test_that_rule_ignores_prose_about_it():
    prose = "It is a slash command, not a shell subcommand — `igni /login` errors."
    assert not _FENCED_SHELL.findall(prose)

"""Which shell commands plan mode runs without asking.

Plan mode used to allow every shell command its mutation regex did not
catch, which made the regex the whole boundary: ``git push``,
``terraform apply``, ``kubectl delete`` and ``curl -o`` all ran unasked,
because none of them is ``rm`` or ``>``. This module inverts that. A
command is read-only only if it is built entirely from an explicit
allow-list of commands that read; anything else is put to the person.

The parse is conservative on purpose — a false "not read-only" costs a
prompt, a false "read-only" lets plan mode write:

* Command substitution (``$(...)``, backticks), process substitution,
  any redirection to or from a file, ``;``, ``&&``, ``||``, ``&``, a
  newline, and anything that does not tokenise cleanly → not read-only.
  The redirects ``2>&1``, ``>&2`` and ``>/dev/null`` are the exceptions:
  they write nowhere.
* A pipe is allowed only between allow-listed commands, so
  ``ls | grep x`` is read-only and ``ls | xargs rm`` is not.
* A leading ``VAR=value``, a path to the command (``/bin/ls``) or a
  wrapper (``sudo``, ``env``, ``xargs``) → not read-only.
* Each allow-listed command also refuses the arguments that make it
  write or run something else: ``find -exec``/``-delete``, ``rg --pre``,
  ``sort -o``, ``git diff --output``, ``git branch -d``, a ``git -c``
  that could name a program, and so on.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable, Sequence

#: Redirects that write to no file, removed before tokenising.
_HARMLESS_REDIRECT_RE = re.compile(r"(?<![^\s|])(?:[12]?>&[12]|&?[12]?>\s*/dev/null)(?=$|[\s|])")

#: Present anywhere in the raw string → not read-only, quoted or not.
_SUBSTITUTION_MARKERS: tuple[str, ...] = ("`", "$(", "<(", ">(", "\n", "\r")

#: Characters ``shlex`` returns as operator tokens with ``punctuation_chars``.
_OPERATOR_CHARS = frozenset("();<>|&")


def _always(_args: Sequence[str]) -> bool:
    return True


def _refuses(*flags: str, prefixes: tuple[str, ...] = ()) -> Callable[[Sequence[str]], bool]:
    """Allowed unless an argument is one of ``flags`` or starts with a prefix."""
    banned = frozenset(flags)

    def check(args: Sequence[str]) -> bool:
        return not any(arg in banned or arg.startswith(prefixes) for arg in args)

    return check


def _refuses_short(letter: str, *long_flags: str) -> Callable[[Sequence[str]], bool]:
    """Allowed unless a short-option cluster contains ``letter`` (``-o``,
    ``-ro``, ``-ofile``) or an argument is one of ``long_flags`` (with or
    without ``=value``)."""

    def check(args: Sequence[str]) -> bool:
        for arg in args:
            if arg == "--":
                break
            if arg.startswith("--"):
                if arg.split("=", 1)[0] in long_flags:
                    return False
            elif arg.startswith("-") and letter in arg[1:]:
                return False
        return True

    return check


_FIND_WRITERS = (
    "-exec",
    "-execdir",
    "-ok",
    "-okdir",
    "-delete",
    "-fprint",
    "-fprint0",
    "-fprintf",
    "-fls",
)

_GIT_OUTPUT = _refuses("--output", "--ext-diff", prefixes=("--output=",))

#: ``git branch`` options that only list.
_GIT_BRANCH_LIST_FLAGS = frozenset(
    {
        "-a",
        "-r",
        "-v",
        "-vv",
        "-l",
        "--list",
        "--all",
        "--remotes",
        "--verbose",
        "--show-current",
        "--no-color",
        "--color",
        "--no-column",
        "--column",
        "-i",
        "--ignore-case",
    }
)
_GIT_BRANCH_VALUE_FLAGS = frozenset(
    {"--contains", "--no-contains", "--merged", "--no-merged", "--points-at"}
)
_GIT_BRANCH_VALUE_PREFIXES = (
    "--sort=",
    "--format=",
    "--contains=",
    "--no-contains=",
    "--merged=",
    "--no-merged=",
    "--points-at=",
    "--color=",
    "--column=",
    "--abbrev=",
)


def _git_branch_lists(args: Sequence[str]) -> bool:
    """``git branch`` that lists. A bare name creates a branch, so a
    positional is a pattern only after ``--list``/``-l``."""
    listing = any(a in ("-l", "--list") for a in args)
    expect_value = False
    for arg in args:
        if expect_value:
            expect_value = False
            continue
        if arg in _GIT_BRANCH_VALUE_FLAGS:
            expect_value = True
            continue
        if arg in _GIT_BRANCH_LIST_FLAGS or arg.startswith(_GIT_BRANCH_VALUE_PREFIXES):
            continue
        if arg.startswith("-"):
            return False
        if not listing:
            return False
    return True


_GIT_SUBCOMMANDS: dict[str, Callable[[Sequence[str]], bool]] = {
    "status": _always,
    "log": _GIT_OUTPUT,
    "diff": _GIT_OUTPUT,
    "show": _GIT_OUTPUT,
    "branch": _git_branch_lists,
    "rev-parse": _always,
    "ls-files": _always,
    # Lists refs. None of its options writes, and a ``%(atom)`` in
    # ``--format`` is expanded by git, not run. Unquoted, the ``(`` is a
    # shell syntax error (bash, dash and zsh all refuse to run the line),
    # so only a quoted or escaped format reaches this check.
    "for-each-ref": _always,
    "blame": _always,
    "grep": _refuses_short("O", "--open-files-in-pager"),
}


def _git_reads(args: Sequence[str]) -> bool:
    """``git <read-only subcommand> ...``. A global option before the
    subcommand is refused: ``-c`` can name a pager or diff program, and
    ``-C``/``--git-dir`` move which repository's config is read."""
    if not args:
        return False
    sub, rest = args[0], args[1:]
    check = _GIT_SUBCOMMANDS.get(sub)
    return check is not None and check(rest)


#: Command name → does it read, given its arguments?
READ_ONLY_COMMANDS: dict[str, Callable[[Sequence[str]], bool]] = {
    "ls": _always,
    "cat": _always,
    "head": _always,
    "tail": _always,
    "grep": _always,
    "egrep": _always,
    "fgrep": _always,
    "rg": _refuses("--pre", prefixes=("--pre=",)),
    "find": _refuses(*_FIND_WRITERS),
    "git": _git_reads,
    "pwd": _always,
    "wc": _always,
    "echo": _always,
    "printf": _always,
    "stat": _always,
    "du": _always,
    "df": _always,
    "basename": _always,
    "dirname": _always,
    "realpath": _always,
    "readlink": _always,
    "which": _always,
    "whoami": _always,
    "uname": _always,
    "diff": _always,
    "cmp": _always,
    "cut": _always,
    "tr": _always,
    "nl": _always,
    "jq": _always,
    "sort": _refuses_short("o", "--output", "--compress-program"),
}


def _tokens(command: str) -> list[str] | None:
    """Shell tokens with operators split out, or ``None`` if it does not parse."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return None


def _segment_reads(argv: Sequence[str]) -> bool:
    if not argv:
        return False
    name, args = argv[0], argv[1:]
    check = READ_ONLY_COMMANDS.get(name)
    return check is not None and check(args)


def is_read_only(command: str) -> bool:
    """Is ``command`` built only from allow-listed read-only commands?"""
    if not command or not command.strip():
        return False
    if any(marker in command for marker in _SUBSTITUTION_MARKERS):
        return False
    tokens = _tokens(_HARMLESS_REDIRECT_RE.sub(" ", command))
    if not tokens:
        return False
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token and set(token) <= _OPERATOR_CHARS:
            if token != "|":  # noqa: S105 — a shell token, not a credential
                return False
            segments.append([])
            continue
        segments[-1].append(token)
    return all(_segment_reads(segment) for segment in segments)


#: Operators that join commands without doing anything themselves. Each
#: side is a command of its own, and a rule has to cover each one.
_JOINERS = frozenset({"|", "&&", "||", ";"})


def command_parts(command: str) -> list[str] | None:
    """The commands a shell line runs, one string each, or ``None``.

    For matching permission rules against: ``Bash(ls *)`` is a rule about
    ``ls``, and ``ls x && rm -rf y`` runs ``rm`` too. It used to match,
    because a rule is an ``fnmatch`` over the whole line and ``*`` matches
    ``&& rm -rf y`` as readily as it matches ``-la``.

    * A line with no operator comes back as itself, unchanged, so an exact
      rule ("Always allow" writes one) still matches it character for
      character.
    * A line of commands joined by ``|``, ``&&``, ``||`` or ``;`` comes back
      as those commands.
    * ``None`` — nothing a rule can vouch for — for command or process
      substitution, a newline, a redirect to or from a file (``2>&1`` and
      ``>/dev/null`` excepted), ``&``, a subshell, or a line that does not
      tokenise.
    """
    if not command or not command.strip():
        return None
    if any(marker in command for marker in _SUBSTITUTION_MARKERS):
        return None
    tokens = _tokens(_HARMLESS_REDIRECT_RE.sub(" ", command))
    if not tokens:
        return None
    if not any(token and set(token) <= _OPERATOR_CHARS for token in tokens):
        return [command]
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token and set(token) <= _OPERATOR_CHARS:
            if token not in _JOINERS:
                return None
            segments.append([])
            continue
        segments[-1].append(token)
    if any(not segment for segment in segments):
        return None
    return [shlex.join(segment) for segment in segments]


__all__ = ["READ_ONLY_COMMANDS", "command_parts", "is_read_only"]

"""The filters a shell line may pipe into without an allow rule of their own.

Since every command on a line needs its own allow rule (see
:meth:`PermissionEvaluator._allow_match`), ``pytest -q 2>&1 | tail -20`` —
the shape a model adds to almost every command — needed ``Bash(tail *)``
beside ``Bash(pytest *)``. A command in this list, used as a pure filter
directly after a ``|``, is allowed without one: it reads the output of the
command before it, and writes to the next one or to the model.

"Pure filter" is checked per command, conservatively:

* no file operand — a filter reads its input, so ``grep foo /etc/shadow``
  or ``head x`` are ordinary commands that need a rule;
* no option that writes (``sort -o``, ``uniq IN OUT``), follows
  (``tail -f``), recurses (``grep -r``), reads another file (``grep -f``,
  ``jq --rawfile``, ``wc --files0-from``) or runs a program (``rg --pre``,
  ``rg -z``, ``sort --compress-program``);
* a long option is matched by any prefix GNU would accept, so ``--outp=x``
  is ``--output=x``.

Not in the list, on purpose: ``tee``, ``xargs``, ``sed``, ``awk`` (each can
write or run something), ``cat`` (a filter only without operands, and never
needed in that position), and the pagers ``less``/``more``, which wait for
a person.

Deny and ask rules still match a filter wherever it is on the line, and the
command before the first ``|`` still needs its own rule.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable, Sequence


def _long_name_hits(
    name: str, flags: frozenset[str], known: frozenset[str] = frozenset()
) -> str | None:
    """The flag in ``flags`` that ``name`` means — itself, or the one it abbreviates.

    GNU ``getopt_long`` accepts an unambiguous prefix, so ``--outp`` is
    ``--output``. Treating a prefix of a refused flag as that flag is the
    conservative reading: at worst a harmless abbreviation needs a rule.
    A name that is itself an option in ``known`` is that option, not an
    abbreviation (``jq --arg`` is not ``--args``).
    """
    if name in flags:
        return name
    if name in known:
        return None
    if len(name) > 3:
        for flag in flags:
            if flag.startswith(name):
                return flag
    return None


def stdin_filter(
    *,
    short_values: str = "",
    long_values: tuple[str, ...] = (),
    long_values_two: tuple[str, ...] = (),
    refuse_short: str = "",
    refuse_long: tuple[str, ...] = (),
    max_positionals: int = 0,
    pattern_short: str = "",
    pattern_long: tuple[str, ...] = (),
) -> Callable[[Sequence[str]], bool]:
    """A check that the arguments make a filter of stdin and nothing more.

    ``short_values`` / ``long_values`` take a value (``-n 5``, ``--lines 5``)
    and ``long_values_two`` take two (``jq --arg name value``), so those
    values are not counted as file operands. ``max_positionals`` is how many
    non-option arguments the command takes that are not files (a pattern,
    a ``tr`` set, a ``jq`` program); one supplied through ``pattern_short``
    / ``pattern_long`` (``grep -e``) uses that allowance up.
    """
    refused_long = frozenset(refuse_long)
    one_value = frozenset(long_values)
    two_values = frozenset(long_values_two)
    patterns = frozenset(pattern_long)
    known = refused_long | one_value | two_values | patterns

    def check(args: Sequence[str]) -> bool:
        positionals = 0
        pattern_given = False
        skip = 0
        for index, arg in enumerate(args):
            if skip:
                skip -= 1
                continue
            if arg == "--":
                positionals += len(args) - index - 1
                break
            if arg.startswith("--"):
                name, has_value = arg.split("=", 1)[0], "=" in arg
                if _long_name_hits(name, refused_long, known):
                    return False
                if _long_name_hits(name, patterns, known):
                    pattern_given = True
                if _long_name_hits(name, two_values, known):
                    skip = 2
                elif _long_name_hits(name, one_value | patterns, known) and not has_value:
                    skip = 1
                continue
            if arg.startswith("-") and arg != "-":
                cluster = arg[1:]
                for position, letter in enumerate(cluster):
                    if letter in refuse_short:
                        return False
                    if letter in pattern_short:
                        pattern_given = True
                    if letter in short_values or letter in pattern_short:
                        if position == len(cluster) - 1:
                            skip = 1
                        break
                continue
            positionals += 1
        allowance = max_positionals - (1 if pattern_given and max_positionals else 0)
        return positionals <= allowance

    return check


_GREP = stdin_filter(
    short_values="mABC",
    long_values=("--max-count", "--after-context", "--before-context", "--context", "--label"),
    refuse_short="rRdDf",
    refuse_long=(
        "--recursive",
        "--dereference-recursive",
        "--directories",
        "--devices",
        "--file",
        "--include",
        "--exclude",
        "--exclude-from",
        "--exclude-dir",
    ),
    max_positionals=1,
    pattern_short="e",
    pattern_long=("--regexp",),
)

#: Command name → are these arguments a pure filter of stdin?
PIPE_FILTERS: dict[str, Callable[[Sequence[str]], bool]] = {
    "head": stdin_filter(short_values="nc", long_values=("--lines", "--bytes")),
    "tail": stdin_filter(
        short_values="ncs",
        long_values=("--lines", "--bytes", "--sleep-interval"),
        refuse_short="fF",
        refuse_long=("--follow", "--pid", "--retry"),
    ),
    "wc": stdin_filter(refuse_long=("--files0-from",)),
    "sort": stdin_filter(
        short_values="ktST",
        long_values=(
            "--key",
            "--field-separator",
            "--buffer-size",
            "--temporary-directory",
            "--parallel",
            "--batch-size",
        ),
        refuse_short="o",
        refuse_long=("--output", "--compress-program", "--files0-from", "--random-source"),
    ),
    # ``uniq IN OUT`` writes OUT, so no operand at all.
    "uniq": stdin_filter(
        short_values="fsw",
        long_values=("--skip-fields", "--skip-chars", "--check-chars", "--group"),
    ),
    "grep": _GREP,
    "egrep": _GREP,
    "fgrep": _GREP,
    "rg": stdin_filter(
        short_values="ABCmMgtTjEr",
        long_values=(
            "--after-context",
            "--before-context",
            "--context",
            "--max-count",
            "--max-columns",
            "--glob",
            "--type",
            "--type-not",
            "--threads",
            "--encoding",
            "--replace",
        ),
        refuse_short="zf",
        refuse_long=("--pre", "--pre-glob", "--search-zip", "--file", "--files"),
        max_positionals=1,
        pattern_short="e",
        pattern_long=("--regexp",),
    ),
    "cut": stdin_filter(
        short_values="bcdf",
        long_values=("--bytes", "--characters", "--delimiter", "--fields", "--output-delimiter"),
    ),
    # Its operands are character sets; ``tr`` never opens a file.
    "tr": stdin_filter(max_positionals=2),
    "nl": stdin_filter(
        short_values="bdfhilnsvw",
        long_values=(
            "--body-numbering",
            "--section-delimiter",
            "--footer-numbering",
            "--header-numbering",
            "--line-increment",
            "--join-blank-lines",
            "--number-format",
            "--number-separator",
            "--starting-line-number",
            "--number-width",
        ),
    ),
    # One operand, the program; a second would be a file.
    "jq": stdin_filter(
        long_values=("--indent",),
        long_values_two=("--arg", "--argjson"),
        refuse_short="fL",
        refuse_long=("--from-file", "--rawfile", "--slurpfile", "--args", "--jsonargs"),
        max_positionals=1,
    ),
}


def is_pipe_filter(command: str) -> bool:
    """Is ``command`` (one command, no operators) a pure filter of stdin?"""
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    if not argv:
        return False
    check = PIPE_FILTERS.get(argv[0])
    return check is not None and check(argv[1:])


__all__ = ["PIPE_FILTERS", "is_pipe_filter", "stdin_filter"]

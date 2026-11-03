"""Plan mode runs a shell command unasked only when every part of it only reads.

:func:`is_read_only` is the boundary plan mode stands on: a false "read-only"
lets a planning turn write, a false "not read-only" costs one prompt. So the
cases pinned here are the ones where a command on the allow-list is made to
write or to run something else by its arguments — ``git`` and ``sort`` most
of all — and the parse failures that must fail closed. Each case names what
it would do if it were let through.
"""

from __future__ import annotations

import pytest

from ember_code.core.config.permission_eval.read_only_shell import is_read_only


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git log --oneline -5",
        "git diff HEAD~1 -- src/",
        "git show HEAD:README.md",
        "git rev-parse --abbrev-ref HEAD",
        "git ls-files",
        "git blame src/app.py",
        "git grep -n TODO",
        "git branch",
        "git branch -a",
        "git branch -vv --no-color",
        "git branch --list 'feat/*'",
        "git branch -l release",
        "git branch --contains abc123",
        "git branch --sort=-committerdate '--format=%(refname)'",
        "git branch --merged main -r",
        "sort -n data.txt",
        "sort -k2 -- -o",
        "rg --pre-glob '*.gz' needle",
        "find . -name '*.py'",
        "ls -la 2>&1 | grep src",
        "cat notes.txt >/dev/null",
        "echo done >&2",
        "jq .name package.json | head -1",
    ],
)
def test_commands_that_only_read_run_unasked(command):
    assert is_read_only(command)


@pytest.mark.parametrize(
    ("command", "what_it_would_do"),
    [
        ("git push origin main", "publish commits"),
        ("git", "nothing to read: a bare git is not a read"),
        ("git -c core.pager=sh status", "name a program for git to run"),
        ("git -C ../other status", "read another repository's config"),
        ("git log --output=log.txt", "write a file"),
        ("git diff --output log.txt", "write a file"),
        ("git show --ext-diff", "run an external diff program"),
        ("git grep -O TODO", "open matches in a pager"),
        ("git grep --open-files-in-pager=vim TODO", "open matches in an editor"),
        ("git branch feature-x", "create a branch"),
        ("git branch -d old", "delete a branch"),
        ("git branch -D old", "force-delete a branch"),
        ("git branch -m old new", "rename a branch"),
        ("sort -o out.txt in.txt", "write a file"),
        ("sort -ro out.txt in.txt", "write a file, flag in a cluster"),
        ("sort --output=out.txt in.txt", "write a file"),
        ("sort --compress-program=sh in.txt", "run a program"),
        ("rg --pre ./script needle", "run a preprocessor"),
        ("rg --pre=./script needle", "run a preprocessor"),
        ("find . -delete", "delete files"),
        ("find . -exec rm {} ;", "run rm"),
        ("ls | xargs rm", "pipe into a command that writes"),
        ("ls && rm -rf build", "chain a write after a read"),
        ("ls; rm x", "chain a write after a read"),
        ("ls > listing.txt", "write a file"),
        ("cat < secrets.txt", "redirect from a file"),
        ("echo $(rm x)", "run a command substitution"),
        ("echo `rm x`", "run a command substitution"),
        ("diff <(ls a) <(ls b)", "run a process substitution"),
        ("ls\nrm x", "run a second line"),
        ("FOO=1 ls", "set the environment of the command"),
        ("/bin/ls", "run a binary by path"),
        ("sudo ls", "run as another user"),
        ("echo 'unterminated", "fail to tokenise"),
        ("", "be empty"),
        ("   ", "be blank"),
        ("ls |", "end in an empty pipe segment"),
        ("2>&1", "be only a redirect"),
    ],
)
def test_commands_that_write_or_run_something_else_are_put_to_the_person(command, what_it_would_do):
    assert not is_read_only(command), what_it_would_do

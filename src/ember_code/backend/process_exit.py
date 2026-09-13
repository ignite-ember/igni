"""Say what a child process's exit code means.

``exit 137`` cost an hour. It is ``128 + 9`` — killed by SIGKILL — and
the JDK behind it had been left half-extracted by an interrupted
download, so every JVM the backend started died instantly, wrote no
stderr, produced no Neo4j log, and reported nothing but a number that
looks like an ordinary failure.

The number was always there. Nothing turned it into a sentence.

:func:`describe_exit` is that sentence, and :func:`format_child_failure`
adds the tail of whatever the child managed to write — because "killed
by signal 9" and "exited 1 saying 'port in use'" need different
responses, and the difference should not require a second debugging
session to establish.
"""

from __future__ import annotations

import signal

#: Signals worth a hint beyond their name. The rest get name + number,
#: which is enough to search for.
_HINTS = {
    signal.SIGKILL: (
        "usually the OS refusing to run it (an invalid or half-extracted binary), "
        "an out-of-memory kill, or an explicit kill -9"
    ),
    signal.SIGSEGV: "crashed (segmentation fault)",
    signal.SIGABRT: "aborted itself",
    signal.SIGTERM: "asked to stop (SIGTERM) — usually our own shutdown",
}


def describe_exit(returncode: int | None) -> str:
    """Turn a return code into something a person can act on.

    Handles both conventions, because both reach us: :mod:`asyncio`
    reports a signal death as a negative number, while a shell (and
    anything that went through one) reports ``128 + signal``.
    """
    if returncode is None:
        return "still running"
    if returncode == 0:
        return "exited cleanly (0)"

    signum: int | None = None
    if returncode < 0:
        signum = -returncode
    elif 128 < returncode < 128 + signal.NSIG:
        signum = returncode - 128

    if signum is not None:
        try:
            sig = signal.Signals(signum)
        except ValueError:
            return f"killed by signal {signum} (exit {returncode})"
        hint = _HINTS.get(sig)
        base = f"killed by {sig.name} (signal {signum}, exit {returncode})"
        return f"{base} — {hint}" if hint else base

    return f"exited with status {returncode}"


def format_child_failure(
    what: str,
    returncode: int | None,
    output: str | None = None,
    *,
    tail_chars: int = 2000,
) -> str:
    """One line a human can read, plus whatever the child said.

    ``output`` is trimmed from the end: a process that dies during
    startup says the useful part last. When it said nothing at all,
    that is itself reported — silence from a dead child is a finding,
    not an absence of one, and writing "(no output)" is what stops the
    next reader assuming the log simply got lost.
    """
    line = f"{what} {describe_exit(returncode)}"
    if not output or not output.strip():
        return f"{line}; it wrote no output"
    trimmed = output.strip()
    if len(trimmed) > tail_chars:
        trimmed = "…" + trimmed[-tail_chars:]
    return f"{line}; last output: {trimmed}"

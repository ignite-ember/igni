"""Workspace trust on the command line: the first-run prompt and ``igni trust``.

Opening a folder never runs its code until the person trusts it
(``core/workspace_trust``). On a terminal, the first ``igni -m`` in a folder
nobody has decided about asks: Trust / Don't trust / This time only. With
nobody to ask — a pipe, CI, a script — the folder runs untrusted unless
``--trust`` is passed (and the organisation's policy allows it), or the
person trusted it earlier.

``igni trust`` records the decision outside a session:

* ``igni trust [PATH]`` — trust the folder (the current one by default);
* ``igni trust --revoke [PATH]`` — forget the decision, so it asks again;
* ``igni trust --list`` — every recorded decision.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from ember_code.core.workspace_trust import (
    GATED_SUMMARY,
    TrustDecision,
    TrustRefused,
    WorkspaceTrust,
)


def _can_prompt() -> bool:
    try:
        return bool(sys.stdin and sys.stdin.isatty() and sys.stderr and sys.stderr.isatty())
    except (AttributeError, OSError, ValueError):
        return False


def _describe(decision: TrustDecision) -> str:
    remote = decision.identity.remote or "none"
    return f"  Folder: {decision.identity.path}\n  Git remote: {remote}"


class SessionTrust:
    """Settle the trust decision for a ``-m`` / pipe run, before its session starts."""

    def __init__(self, project_dir: Path, *, pipe: bool, trust_flag: bool, quiet: bool) -> None:
        self._project_dir = project_dir
        self._pipe = pipe
        self._trust_flag = trust_flag
        self._quiet = quiet
        self._trust = WorkspaceTrust.shared()

    def settle(self) -> bool:
        """Ask or apply ``--trust`` as needed. True when the folder became
        trusted, so the caller reloads settings with its config."""
        before = self._trust.decide(self._project_dir)
        if before.trusted:
            return False
        if self._trust_flag:
            after = self._trust.grant_for_run(self._project_dir)
            if not after.trusted:
                self._notice(after)
            return after.trusted
        if before.needs_answer and not self._pipe and _can_prompt():
            return self._ask(before)
        self._notice(before)
        return False

    def _ask(self, decision: TrustDecision) -> bool:
        click.echo(
            "\nTrust this folder? It can run code: tools, hooks, workflows, "
            "language servers…\n" + _describe(decision) + "\n\n"
            f"Until you trust it, igni leaves off its {GATED_SUMMARY}.",
            err=True,
        )
        answer = click.prompt(
            "  [t] Trust  [d] Don't trust  [o] This time only",
            type=click.Choice(["t", "d", "o"], case_sensitive=False),
            default="d",
            show_choices=False,
            err=True,
        ).lower()
        if answer == "d":
            self._trust.distrust(self._project_dir)
            return False
        try:
            self._trust.trust(self._project_dir, remember=answer == "t")
        except TrustRefused as exc:  # policy changed underneath; say so
            click.echo(f"  Not trusted: {exc}", err=True)
            return False
        return True

    def _notice(self, decision: TrustDecision) -> None:
        if self._quiet:
            return
        hint = (
            " Run `igni trust` to trust it, or pass --trust for this run."
            if decision.can_trust
            else ""
        )
        click.echo(
            f"igni: {decision.identity.path} is not trusted — its {GATED_SUMMARY} "
            f"are off. {decision.reason}{hint}",
            err=True,
        )


@click.command("trust")
@click.argument(
    "path",
    required=False,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option("--revoke", is_flag=True, help="Forget the decision for the folder; igni asks again.")
@click.option("--list", "list_", is_flag=True, help="List every folder you have decided about.")
def trust_command(path: Path | None, revoke: bool, list_: bool) -> None:
    """Trust a folder to run its own code (tools, hooks, workflows, language servers…).

    PATH defaults to the current folder.
    """
    # Loads settings for the organisation's policy (``workspace_trust`` is
    # read from the managed file, the group policy and the environment).
    from ember_code.cli.invocation import load_settings_from_options
    from ember_code.cli.options import CliOptions

    load_settings_from_options(CliOptions())
    service = WorkspaceTrust.shared()

    if list_:
        entries = service.entries()
        if not entries:
            click.echo("No folders trusted or distrusted yet.")
            return
        for entry in entries:
            click.echo(f"{entry.state:<10} {entry.path}  [{entry.remote or 'no remote'}]")
        return

    folder = path or Path.cwd()
    if revoke:
        decision = service.revoke(folder)
        click.echo(f"Trust revoked for {decision.identity.path}; igni will ask again.")
        return
    try:
        decision = service.trust(folder)
    except TrustRefused as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Trusted {decision.identity.path}\n" + _describe(decision))


__all__ = ["SessionTrust", "trust_command"]

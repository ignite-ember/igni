"""Context-related slash commands: ``/output-style``, ``/compact``, ``/ctx``.

Extracted from :mod:`ember_code.backend.command_handler` — three
commands that inspect / mutate the session's context surface:

* ``/output-style`` — list / set / show the active output style
  (CC parity, row 52). Hot-patches the agent's instructions so
  the next turn picks up the new tone without a rebuild.
* ``/compact`` — force a compaction pass (drop conversation
  runs, keep the summary + system floor).
* ``/ctx`` — break down the current context counter into
  conversation vs. floor, so users can see why `/compact`
  doesn't drop the meter to zero.

Output-style body files live at ``.ember/output-styles/<name>.md``
(project) or ``~/.ember/output-styles/<name>.md`` (user), plus
the ``.claude/`` equivalents when cross-tool reads are enabled.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ember_code.backend.command_handler import CommandHandler, CommandResult


async def cmd_output_style(handler: "CommandHandler", args: str) -> "CommandResult":
    """List / set / show the active output style."""
    from ember_code.backend import command_handler as _handler
    from ember_code.protocol.messages import CommandResultKind

    CommandResult = _handler.CommandResult
    session = handler._session
    styles = session.output_styles
    active = session._active_output_style

    normalized = args.strip()
    cmd, _, rest = normalized.partition(" ")
    cmd = cmd.lower()

    if normalized in ("", "list"):
        if not styles:
            return CommandResult(
                kind=CommandResultKind.INFO,
                content=(
                    "No output styles configured. Drop a markdown file at "
                    "`.ember/output-styles/<name>.md` (frontmatter: `name`, "
                    "`description`; body is the system-prompt extension)."
                ),
            )
        lines = ["**Output styles**", ""]
        for name in sorted(styles):
            marker = " (active)" if name == active else ""
            desc = styles[name].description or "_(no description)_"
            lines.append(f"- `{name}`{marker} — {desc}")
        lines.append("")
        lines.append("Switch with `/output-style <name>`.")
        return CommandResult(
            kind=CommandResultKind.MARKDOWN,
            content="\n".join(lines),
        )

    if normalized in ("status", "show"):
        return CommandResult(
            kind=CommandResultKind.INFO,
            content=f"Active output style: **{active or '(none)'}**",
        )

    # Treat anything else as a style name to switch to —
    # ``/output-style explanatory`` and the explicit
    # ``/output-style set explanatory`` both land here.
    target_name = rest.strip() if cmd == "set" else normalized

    if not target_name:
        return CommandResult.error(
            "Usage: /output-style <name> (or `/output-style list` to see options)."
        )

    status_line = session.set_output_style(target_name)
    if status_line.startswith("Error"):
        return CommandResult.error(status_line)
    return CommandResult(
        kind=CommandResultKind.INFO,
        content=status_line,
    )


async def cmd_compact(handler: "CommandHandler") -> "CommandResult":
    """Force a compaction pass. Returns the summary as a separate
    field so the FE can render a structured "Context compacted"
    card with the model-generated summary as the body."""
    from ember_code.backend import command_handler as _handler
    from ember_code.protocol.messages import CommandAction, CommandResultKind

    CommandResult = _handler.CommandResult
    status, summary = await handler._session.force_compact()
    return CommandResult(
        kind=CommandResultKind.ACTION,
        action=CommandAction.COMPACT,
        content=status,
        display_content=summary,
    )


async def cmd_ctx(handler: "CommandHandler") -> "CommandResult":
    """Break down the current ctx counter into floor vs conversation.

    ``/compact`` only clears the conversational runs — system
    prompt, tool schemas, project rules, memories and the
    injected session summary stay. ``/ctx`` shows the split so
    the user can see why the meter doesn't drop to zero after
    compaction.
    """
    from ember_code.backend import command_handler as _handler
    from ember_code.protocol.messages import CommandResultKind

    CommandResult = _handler.CommandResult
    b = await handler._session.context_breakdown()
    total = b.total
    runs = b.runs
    floor = b.floor
    pct = (runs / total * 100.0) if total else 0.0
    lines = [
        "**Context breakdown**",
        "",
        f"- **Total:** {total:,} tokens",
        f"- **Conversation (runs):** {runs:,} tokens ({pct:.1f}% of total)",
        f"- **Floor (system + tools + rules + memories + summary):** {floor:,} tokens",
        "",
        "`/compact` only clears the conversation portion — the floor "
        "is rebaked into every prompt and cannot be compacted away.",
    ]
    return CommandResult(
        kind=CommandResultKind.MARKDOWN,
        content="\n".join(lines),
    )

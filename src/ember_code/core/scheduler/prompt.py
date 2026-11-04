"""Scheduled-run prompt wrapper.

``SchedulerController.execute`` used to be ``team.arun(description)``: the
task's description *was* the prompt, arriving in the same shape as a message
somebody had just typed. Nothing told the agent that this fire came from the
clock, that the person who wrote the description did so hours or days earlier,
or that nobody is watching the stream — so the agent could reasonably ask a
clarifying question, and the answer would never come. The run then ends having
done nothing, with a question nobody reads.

This is the same problem ``/loop`` solved with
:func:`ember_code.core.loop.prompt.wrap_iteration_prompt` and the message queue
solved with ``[USER MESSAGE WHILE YOU WERE WORKING]``: the model cannot infer the
delivery channel from the text, so the channel says so itself. The wrapper keeps
the house shape — angle-bracket tag, terse meta, blank line, the original text
verbatim — for the reason recorded next to the loop wrapper: Textual parses
``[...]`` as markup and a square-bracket wrapper once took the whole TUI down
with a ``MarkupError``.

What it tells the agent is deliberately narrow. Not "you have no permissions" —
a scheduled run has exactly the tools any other run has. Only that there is no
one to ask, and that finishing or reporting beats waiting.
"""

from __future__ import annotations


def wrap_scheduled_prompt(description: str, *, task_id: str | None = None) -> str:
    """Wrap a scheduled task's description as an autonomous run.

    Args:
        description: What the user asked for when they scheduled the task.
            Returned verbatim inside the tag — it is the actual instruction and
            nothing here should compete with it.
        task_id: The store's id for this task, when the caller has it. Included
            so a run that wants to report against the task can name it.

    Returns:
        The wrapped prompt. Shape::

            <scheduled-run[ task="ID"]>
            This is a scheduled task firing on its own. Nobody is
            watching and no one will answer a question, so do not ask
            one — make the most reasonable assumption, say which
            assumption you made, and finish.

            If the task cannot be completed, report what stopped it.
            An unfinished report is useful; a question is not.

            <the description>
            </scheduled-run>
    """
    attr = f' task="{task_id}"' if task_id else ""
    return (
        f"<scheduled-run{attr}>\n"
        f"This is a scheduled task firing on its own. Nobody is watching and no "
        f"one will answer a question, so do not ask one — make the most "
        f"reasonable assumption, say which assumption you made, and finish.\n"
        f"\n"
        f"If the task cannot be completed, report what stopped it. An "
        f"unfinished report is useful; a question is not.\n"
        f"\n"
        f"{description}\n"
        f"</scheduled-run>"
    )

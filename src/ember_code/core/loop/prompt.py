"""Per-iteration prompt wrapper.

The ``/loop`` primitive re-fires the user's prompt every iteration.
Without a wrapper, the agent treats each fire as a fresh
conversation turn — which is fine right up until the model
decides "is it OK if I continue to the next item?" is a reasonable
question to ask. A user typing the answer (``yes``) then triggers
the cancel-on-user-input guard and kills the loop.

The wrapper below makes the loop context explicit to the agent on
*every* iteration: it's autonomous, the user is not available to
answer questions, and tool-permission prompts are the only
legitimate interactive surface. Keeping the wrapper terse —
single-line meta + blank line + original prompt — keeps the chat
display readable while still landing the instruction in the
agent's input.
"""

from __future__ import annotations


def wrap_iteration_prompt(prompt: str, iteration: int, total: int | None = None) -> str:
    """Prepend a one-line meta-instruction to an iteration's prompt.

    Called from every site that feeds a prompt into an iteration —
    :py:meth:`Session.advance_loop`, :py:meth:`Session.resume_loop`,
    and ``_cmd_loop`` (for iteration 1 fired via the
    ``run_prompt`` action).

    The wrapper uses angle-bracket XML-style tags rather than
    ``[...]`` square brackets, because the wrapped prompt is
    rendered by Textual's Static widget on the FE — and Textual
    parses ``[...]`` as markup, treating ``[/foo]`` as a closing
    tag. A wrapper like ``[/loop iteration 1/30]`` blew up the
    whole TUI with a ``MarkupError`` ("closing tag does not match
    any open tag") the first time it tried to render. Angle
    brackets pass through markup unchanged while modern LLMs
    still recognize the XML-style hint.

    Args:
        prompt: The user's original ``/loop`` body. Returned
            verbatim inside the tag.
        iteration: 1-based index of the iteration being fired.
        total: Intended total number of iterations, when known.
            Sent to the model only when the user explicitly capped
            the run (``/loop N <prompt>``); for the default
            implicit cap we pass ``None`` so the model doesn't
            try to pace itself against a fake target — the cap
            is just a safety net, not a target.

    Returns:
        The wrapped prompt. Shape::

            <loop-iteration index="N" [total="M"]>
            Autonomous loop iteration — do not ask the user;
            perform one unit of work and stop. Tool-permission
            prompts are the only legitimate user interaction.

            <original prompt>
            </loop-iteration>

        ``total`` attribute is omitted when ``total is None``.
    """
    total_attr = f' total="{total}"' if total is not None else ""
    # Coach the model on the legitimate exits and the
    # ``loop_set_total`` channel — the user's natural-language
    # prompt almost never matches ``/loop N <prompt>`` literal
    # syntax, so the panel can't display N/total until the agent
    # itself announces the count. We surface this on every
    # iteration so the model is reminded even if iteration 1
    # didn't have enough info to call it yet.
    return (
        f'<loop-iteration index="{iteration}"{total_attr}>\n'
        f"Autonomous loop iteration — do not ask the user; "
        f"perform one unit of work and stop. Tool-permission "
        f"prompts are the only legitimate user interaction.\n"
        f"\n"
        f"When you can determine the total number of items "
        f"(e.g. after listing files or parsing the input), call "
        f"loop_set_total(N) once so the panel renders progress as "
        f"N/total. Call loop_stop() when all work is done — don't "
        f"keep looping just because the safety cap hasn't been hit.\n"
        f"\n"
        f"{prompt}\n"
        f"</loop-iteration>"
    )

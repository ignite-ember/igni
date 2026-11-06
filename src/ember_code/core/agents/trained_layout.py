"""The system-prompt layout igni-1.0 was trained on, kept under Agno 3.

igni-1.0 was fine-tuned on requests igni sent through Agno 2.6, and its
corpus is checked against live renders (the fine-tuning repo's
``verify_corpus`` / ``refresh_corpus_prompts``). Agno 3 rewrote two parts
of the system message every agent sends, and both are pinned back here so
a request is byte-identical to what the model saw in training:

**The instruction list.** Agno 2.6 bulleted every instruction when there
were several — ``- {instruction}\\n`` each, whatever its length — and put
the next section straight after. Agno 3 (``agno.utils.message.render_instructions``)
bullets only one-line instructions, joins multi-line ones as blocks with
blank lines between, and always ends the section with a blank line. igni's
instructions are long multi-line blocks, so every agent's prompt changed:
the opening ``- You are igni`` lost its bullet, and blank lines appeared
between sections.

A single instruction is rendered the same by both. So igni renders the
list itself, Agno 2.6's way, and hands Agno one instruction. The one byte
Agno 3 still adds — a blank line after the section, where 2.6 had none —
is absorbed by the section 2.6 put right after it: the
``<additional_information>`` block that ``markdown=True`` produces. igni
writes that block itself, at the end of the one instruction, and turns
``markdown`` off so Agno does not write it again. Agno 3's ``markdown``
does nothing else (``agent/_messages.py``: it only adds that line).

**The user-memory block.** Agno 2.6's ``UserMemoryStore.build_context``
put the ``update_user_memory`` guidance inside ``<user_memory>``; Agno 3
moved it to a separate ``<user_memory_instructions>`` block ahead of it
(``instructions()``). ``ember_code.core.learn.TrainedUserMemoryStore``
renders the 2.6 block and contributes no separate guidance.

Not pinned, and why: Agno 3 also rewrote the team leader's prompt and the
``delegate_task_to_member`` description, and the memory-extraction call
``update_user_memory`` makes. Neither appears in igni-1.0's corpus (no
trace carries either system prompt), so there is no trained layout to
keep; pinning them would mean owning Agno's team prompt.
"""

from __future__ import annotations

from typing import Any

#: What ``markdown=True`` adds to ``<additional_information>``, verbatim.
MARKDOWN_LINE = "Use markdown to format your answers."


class TrainedLayout:
    """Agent kwargs adjusted so Agno 3 renders the instructions as Agno 2.6 did."""

    @staticmethod
    def instructions(
        instructions: list[Any] | None, *, markdown: bool
    ) -> tuple[list[Any] | None, bool]:
        """``(instructions, markdown)`` to hand Agno 3 for Agno 2.6's rendering.

        Zero or one instruction renders the same in both, as does a list
        Agno 2.6 would have put inside ``<instructions>`` tags — igni does
        not use the tags. Without ``markdown`` there is no following
        section to absorb Agno 3's trailing blank line, so the list is
        left to Agno; no igni agent is built that way.
        """
        if not instructions or len(instructions) == 1 or not markdown:
            return instructions, markdown
        rendered = "".join(f"- {instruction}\n" for instruction in instructions)
        rendered += f"<additional_information>\n- {MARKDOWN_LINE}\n</additional_information>"
        return [rendered], False


__all__ = ["MARKDOWN_LINE", "TrainedLayout"]

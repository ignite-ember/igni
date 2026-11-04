"""What the model reads when the person says no to a tool call.

Every refusal the model was trained on is a ``Blocked: …`` sentence naming
the tool in quotes — ``Blocked: permission policy denied 'edit_file'.``
1,166 times in the fine-tuning corpus, never anything else. A rejection in
the approval dialog reached the model as the bare ``User denied``, a string
the corpus does not contain once; against igni-1.0-4b the run took it for a
glitch and sent the same write through another tool eleven times.

The wording is the one the permission pipeline already gives when the person
declines a call asked about mid-run, so one event reads one way whichever
path asked. Only what happened, no advice: an earlier "Do not retry it
another way" had the model telling the person a policy had stopped it.
"""

from __future__ import annotations


def declined_note(tool_name: str) -> str:
    """The tool result for a call the person rejected."""
    if not tool_name:
        return "Blocked: the person declined this call."
    return f"Blocked: the person declined '{tool_name}'."

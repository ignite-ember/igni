"""A rejection in the approval dialog reaches the model as a trained ``Blocked:`` refusal.

The fine-tuning corpus holds 1,166 refusals of the shape
``Blocked: permission policy denied 'edit_file'.`` and no bare ``User denied``.
That bare string is what a rejected call used to return; against igni-1.0-4b
the run sent the same write through another tool eleven times before asking.
"""

from __future__ import annotations

import re

from ember_code.core.approvals.declined import declined_note

#: The corpus's refusal shape: ``Blocked:``, the tool in single quotes, a full stop.
TRAINED = re.compile(r"^Blocked: [^\n]*'[a-z_]+'[^\n]*\.$")


def test_the_note_has_the_trained_shape():
    assert TRAINED.match(declined_note("edit_file"))
    assert declined_note("edit_file") == "Blocked: the person declined 'edit_file'."


def test_a_call_without_a_name_still_reads_as_a_refusal():
    assert declined_note("") == "Blocked: the person declined this call."

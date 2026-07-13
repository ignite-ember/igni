"""Shared helpers for modal/overlay widgets.

Pulled out during the per-dialog extraction pass so the individual
dialog modules (``_login_widget.py``, ``_permission_dialog.py``,
``_session_picker.py``, ``_model_picker.py``) can share the
``_is_inside`` widget-tree walker without importing back from
``_dialogs.py`` (which would create a needless parent → child →
parent cycle).
"""

from __future__ import annotations

from textual.widget import Widget


def _is_inside(target: Widget, container: Widget) -> bool:
    """True if ``target`` is a descendant of ``container``.

    Textual's ``Widget`` doesn't expose ``is_descendant_of``, so we walk
    the parent chain ourselves. The previous code called the missing
    method, swallowed the AttributeError, and silently dropped clicks.
    """
    node = getattr(target, "parent", None)
    while node is not None:
        if node is container:
            return True
        node = getattr(node, "parent", None)
    return False

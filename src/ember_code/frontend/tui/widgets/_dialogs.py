"""Backwards-compat re-export shim for the dialog widgets.

Historically this module held every modal/overlay dialog in one
file (641 LoC). The per-widget split (iters 19, 31, 32, 33, 34)
moved each dialog to its own module. This file remains only so
that existing private-path imports keep working:

    from ember_code.frontend.tui.widgets._dialogs import PermissionDialog

Canonical locations:
    - :mod:`_session_info`     — SessionInfo schema (Pattern 7)
    - :mod:`_login_widget`     — LoginWidget dialog
    - :mod:`_model_picker`     — ModelPickerWidget dialog
    - :mod:`_session_picker`   — SessionPickerWidget dialog
    - :mod:`_permission_dialog`— PermissionDialog dialog
    - :mod:`_dialogs_common`   — shared ``_is_inside`` helper

New code should import from those directly. This shim exists to
keep patches / tests written against the old dotted path working
without a mass find-and-replace across the codebase.
"""

from ember_code.frontend.tui.widgets._dialogs_common import _is_inside
from ember_code.frontend.tui.widgets._login_widget import LoginWidget
from ember_code.frontend.tui.widgets._model_picker import ModelPickerWidget
from ember_code.frontend.tui.widgets._permission_dialog import PermissionDialog
from ember_code.frontend.tui.widgets._session_info import SessionInfo
from ember_code.frontend.tui.widgets._session_picker import SessionPickerWidget

__all__ = [
    "LoginWidget",
    "ModelPickerWidget",
    "PermissionDialog",
    "SessionInfo",
    "SessionPickerWidget",
    "_is_inside",
]

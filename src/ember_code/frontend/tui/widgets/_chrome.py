"""Backwards-compat re-export shim for the chrome widgets.

Historically this module held every app-chrome widget in one file
(584 LoC). The per-widget split (iters 35–38) moved each widget
to its own module. This file remains only so that existing
private-path imports keep working:

    from ember_code.frontend.tui.widgets._chrome import StatusBar

Canonical locations:
    - :mod:`_welcome_banner`  — WelcomeBanner
    - :mod:`_tip_bar`         — TipBar
    - :mod:`_update_bar`      — UpdateBar (+ `_upgrade_command` helper)
    - :mod:`_spinner_widget`  — SpinnerWidget
    - :mod:`_queue_panel`     — QueuePanel
    - :mod:`_status_bar`      — StatusBar

New code should import from those directly. This shim exists to
keep patches / tests written against the old dotted path working
without a mass find-and-replace across the codebase.
"""

from ember_code.frontend.tui.widgets._queue_panel import QueuePanel
from ember_code.frontend.tui.widgets._spinner_widget import SpinnerWidget
from ember_code.frontend.tui.widgets._status_bar import StatusBar
from ember_code.frontend.tui.widgets._tip_bar import TipBar
from ember_code.frontend.tui.widgets._update_bar import UpdateBar
from ember_code.frontend.tui.widgets._welcome_banner import WelcomeBanner

__all__ = [
    "QueuePanel",
    "SpinnerWidget",
    "StatusBar",
    "TipBar",
    "UpdateBar",
    "WelcomeBanner",
]

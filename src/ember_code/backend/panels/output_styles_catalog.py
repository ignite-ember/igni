"""Output-styles catalog controller.

Owns the output-styles picker chip: discovered styles + the
currently-applied one. Uses :attr:`Session.active_output_style`
(the existing public property) instead of the
``session._active_output_style`` reach-in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ember_code.backend.schemas_panels import OutputStyleInfo, OutputStylesResult

if TYPE_CHECKING:
    from ember_code.core.session import Session


class OutputStylesCatalog:
    """Snapshot of discovered output styles for the picker chip."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def snapshot(self) -> OutputStylesResult:
        """Discovered styles + the currently-active one."""
        styles = getattr(self._session, "output_styles", {}) or {}
        active = self._session.active_output_style or ""
        return OutputStylesResult(
            active=active,
            styles=[
                OutputStyleInfo(name=s.name, description=s.description)
                for s in sorted(styles.values(), key=lambda s: s.name)
            ],
        )

"""Model-switching logic — extracted from :class:`BackendServer`.

The previous ``BackendServer.switch_model`` was a 46-LoC method
reaching into five private-attributes of :class:`Session`
(``settings.models.default``, ``main_team``, ``_build_main_agent``,
``session_id``) plus writing to two persistence layers (user-level
config file + per-session prefs table). Extracting the logic into a
dedicated class keeps the god-class small and gives the switch
concern one testable owner.

The controller takes :class:`Session` + :class:`SessionPreferencesStore`
+ :class:`UserConfigStore` — three real collaborator objects rather
than a mix of state + a bound-method callback (the previous
``save_default_model`` callable pattern only existed to route around
a module-level shim that has now been deleted).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ember_code.protocol import messages as msg

if TYPE_CHECKING:
    from ember_code.core.config.user_config_store import UserConfigStore
    from ember_code.core.session import Session
    from ember_code.core.session.session_preferences import SessionPreferencesStore

logger = logging.getLogger(__name__)


class ModelSwitcher:
    """Switch the active model + persist both user-level and
    per-session preferences."""

    def __init__(
        self,
        session: Session,
        session_prefs: SessionPreferencesStore,
        user_config_store: UserConfigStore,
    ) -> None:
        self._session = session
        self._session_prefs = session_prefs
        self._user_config_store = user_config_store

    def switch(self, model_name: str) -> msg.Info:
        """Switch the active model and persist the choice.

        Two layers of persistence so the choice survives both an
        app restart AND a session resume:

        * **User-level default** — written to ``~/.ember/config.yaml``
          via :class:`UserConfigStore` so any new session opened
          next launch uses this model. Best-effort: a save failure
          is logged but doesn't fail the switch (the in-memory
          state is already updated).
        * **Per-session preference** — written to ``state.db``'s
          ``ember_session_preferences`` table keyed by session_id.
          ``--continue`` consults this on startup.
        """
        old_name = self._session.settings.models.default
        # Registry rows are heterogeneous — typed
        # :class:`ModelRegistryEntry` from cloud discovery vs. raw
        # dicts from user YAML. ``_row_has_vision`` normalises the
        # vision-attribute lookup across both shapes.
        registry = self._session.settings.models.registry
        old_vision = self._row_has_vision(registry.get(old_name))
        new_vision = self._row_has_vision(registry.get(model_name))

        self._session.settings.models.default = model_name
        self._session.rebuild_main_team()

        # User-level persistence.
        try:
            self._user_config_store.set_default_model(model_name)
        except Exception as exc:
            logger.warning("failed to persist model choice to user config: %s", exc)

        # Per-session persistence.
        try:
            self._session_prefs.set_model(self._session.session_id, model_name)
        except Exception as exc:
            logger.debug("failed to persist per-session model preference: %s", exc)

        note = f"Switched to {model_name}"
        # Warn if switching from vision to non-vision with media in
        # history.
        if old_vision and not new_vision:
            note += (
                "\nNote: previous messages may contain images/files. "
                "Use /clear to reset if you get errors."
            )
        return msg.Info(text=note)

    @staticmethod
    def _row_has_vision(row: object) -> bool:
        """Read the ``vision`` flag from a registry row, tolerating
        both the typed :class:`ModelRegistryEntry` shape and the raw
        dict shape (user YAML). Missing / non-conformant rows are
        treated as non-vision."""
        if row is None:
            return False
        if hasattr(row, "vision"):
            return bool(getattr(row, "vision", False))
        if isinstance(row, dict):
            return bool(row.get("vision"))
        return False

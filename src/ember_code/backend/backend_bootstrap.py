"""Composition-root builder for :class:`BackendServer`.

Extracted from ``BackendServer.__init__`` — the previous body was
an imperative sequence of state-db lookups, session-preferences
reads, model-restore branches, ``Session`` construction, and
inline imports. Every one of those imports lived under a "late-
import to avoid cycle" comment; the fix is to move the *class
boundary*, not to hide the import.

:class:`BackendBootstrap` accepts the four caller-supplied inputs
(``settings``, ``project_dir``, ``resume_session_id``,
``additional_dirs``) and exposes the built collaborators as
attributes:

* :attr:`session` — the :class:`Session`.
* :attr:`settings` — the ``Settings`` passed in.
* :attr:`hitl_store` — :class:`PendingRequirementsStore`.
* :attr:`hitl_tracer` — :class:`HITLTracer` built from settings.
* :attr:`pending_store` — :class:`PendingMessageStore`.
* :attr:`session_prefs` — :class:`SessionPreferencesStore`.
* :attr:`user_config_store` — :class:`UserConfigStore`.

``BackendServer.__init__`` shrinks to a one-liner + a controller-
registry build. Tests that construct via ``BackendServer.__new__``
bypass this file entirely — the seam is preserved.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ember_code.backend.hitl_tracer import HITLTracer
from ember_code.backend.pending_requirements_store import PendingRequirementsStore
from ember_code.core.code_index.paths import state_db_path
from ember_code.core.config.user_config_store import UserConfigStore
from ember_code.core.session.pending_messages import PendingMessageStore
from ember_code.core.session.session_preferences import SessionPreferencesStore

if TYPE_CHECKING:
    from ember_code.core.config.settings import Settings


class BackendBootstrap:
    """Builds every long-lived collaborator :class:`BackendServer`
    needs before the controller registry can fire.

    Instantiate once per production ``BackendServer`` — every
    attribute is a one-shot construction. Test fixtures that build
    ``BackendServer`` via ``__new__`` never touch this class.
    """

    def __init__(
        self,
        settings: Settings,
        project_dir: Path | None = None,
        resume_session_id: str | None = None,
        additional_dirs: list[Path] | None = None,
    ) -> None:
        resolved_project_dir = project_dir or Path.cwd()

        # Per-session prefs need to be consulted BEFORE the Session
        # builds its main team, since the team binds whatever model
        # is in ``settings.models.default`` at construction time.
        self.session_prefs = SessionPreferencesStore(
            state_db_path(resolved_project_dir, data_dir=settings.storage.data_dir),
        )
        if resume_session_id:
            persisted_model = self.session_prefs.get_model(resume_session_id)
            if persisted_model and persisted_model in settings.models.registry:
                settings.models.default = persisted_model

        # Late-import ``Session`` — resolved at call time so
        # ``patch('ember_code.core.session.Session', ...)`` in
        # ``tests/test_model_persistence.py`` continues to
        # intercept construction.
        from ember_code.core.session import Session  # noqa: PLC0415 — mock-patch target

        self.session = Session(
            settings,
            project_dir=project_dir,
            resume_session_id=resume_session_id,
            additional_dirs=additional_dirs,
        )
        self.settings = settings

        # HITL pause state lives on a dedicated store, not two raw
        # dicts. Both the pending-user bucket AND the evaluator-
        # auto-resolved bucket are behind one class so the "pending
        # XOR auto_resolved" invariant has one place to live.
        self.hitl_store = PendingRequirementsStore()
        self.hitl_tracer = HITLTracer.from_settings(settings)

        # Pre-persist user messages BEFORE handing them to Agno.
        self.pending_store = PendingMessageStore(
            state_db_path(
                self.session.project_dir,
                data_dir=settings.storage.data_dir,
            ),
        )

        # Injected into ModelSwitcher by the controller registry —
        # moved from an inline import in ``BackendServer.model_switcher``
        # to here so the module boundary carries the dependency.
        self.user_config_store = UserConfigStore()

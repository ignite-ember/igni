"""Learning-machine coordinator for :class:`Session`.

Extracted from :mod:`ember_code.core.session.core` — the
``_learning`` field plus the four public methods that own the
learning-context injection / extraction flow (``inject_learnings``,
``_inject_learnings``, ``extract_learnings``) plus the two
learning-machine accessors (``learning``, ``learning_machine``)
migrate to one class here.

Mirrors the sibling shape of :class:`SessionMemoryManager` and
:class:`SessionKnowledgeManager` — the three "context" managers
now all live next to each other in ``core/session/`` and share
the same "narrow constructor + one-line forwarder from Session"
contract.

Rule 6 (oop_offender #9): a coordinator class replaces the six
sprawled fields / methods on the Session god-class.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from agno.models.message import Message as AgnoMessage

from ember_code.core.config.models import ModelRegistry
from ember_code.core.config.settings import Settings

logger = logging.getLogger(__name__)


class SessionLearningManager:
    """Owns the :class:`~agno.learn.LearningMachine` for one session.

    Constructor takes the pre-constructed learning machine plus
    a ``main_team_ref`` / ``session_id_ref`` / ``user_id_ref``
    closure so the manager doesn't reach back into
    :class:`Session` — the injection / extraction paths need the
    live team + ids, and closures let the manager tolerate the
    team being rebuilt under plugin-reload / compact / MCP-refresh.

    The learning machine itself is built by :class:`Session`
    (calling :func:`create_learning_machine` from its own module
    namespace) so test-patches at
    ``ember_code.core.session.core.create_learning_machine``
    still intercept.
    """

    def __init__(
        self,
        settings: Settings,
        db: Any,
        user_id_ref: Callable[[], str],
        session_id_ref: Callable[[], str],
        main_team_ref: Callable[[], Any],
        learning: Any = None,
        boot_reason: str = "",
    ) -> None:
        self._settings = settings
        self._db = db
        self._user_id_ref = user_id_ref
        self._session_id_ref = session_id_ref
        self._main_team_ref = main_team_ref
        self._learning = learning
        # Surface the boot-time reason ONCE at a higher level (the
        # factory in ``learn.py`` stays silent so operators aren't
        # spammed on every session). ``learning is None`` combined
        # with a non-empty reason means the factory tried and
        # failed / opted out — worth a WARN so log-scrapers can
        # alert. Silence when learning is present or reason is
        # empty (test doubles that pass a fake LM directly).
        if learning is None and boot_reason:
            logger.warning("LearningMachine unavailable: %s", boot_reason)

    @property
    def machine(self) -> Any:
        """The Agno :class:`LearningMachine` for this session, or
        ``None`` when learning is disabled."""
        return self._learning

    @property
    def effective_machine(self) -> Any:
        """Effective machine for user-facing recall commands.

        Prefers the machine attached to the current
        :attr:`main_team` (Agno's own property triggers lazy init
        for teams that support it) and falls back to the
        session-owned :attr:`machine` when the team doesn't expose
        one. Kept lenient — teams without a learning machine
        return ``None`` rather than raising ``AttributeError``.
        """
        team_learning = getattr(self._main_team_ref(), "learning_machine", None)
        if team_learning is not None:
            return team_learning
        return self._learning

    async def inject(self) -> None:
        """Inject learning context into the main agent's
        instructions.

        Silent on failure (opportunistic path): a DB blip, missing
        model, or learning-machine downtime must not block the
        run. Failures log at DEBUG rather than propagate.
        """
        learning = self._learning
        if learning is None:
            return
        if learning.model is None:
            learning.model = ModelRegistry(self._settings).get_model()
        if learning.db is None:
            learning.db = self._db
        try:
            ctx = await learning.abuild_context(
                user_id=self._user_id_ref(),
                session_id=self._session_id_ref(),
            )
        except Exception as exc:  # noqa: BLE001 — opportunistic path
            logger.debug("Learning context build failed: %s", exc)
            return
        main_team = self._main_team_ref()
        if ctx and main_team.instructions:
            # Remove old learning context and add fresh
            main_team.instructions = [
                i
                for i in main_team.instructions
                if not i.startswith("## What I Know About You")
                and not i.startswith("## User Profile")
            ]
            main_team.instructions.append(ctx)

    async def extract(self, user_msg: str, assistant_msg: str) -> None:
        """Push a completed turn into the learning pipeline as a
        background task.

        Owns the ``learning is None`` guard plus the
        :func:`asyncio.create_task` launch — the caller awaits
        this method to schedule the work; the actual
        ``aprocess`` runs detached so the RPC returns immediately.

        Silent on failure by design.
        """
        learning = self._learning
        if learning is None:
            return

        messages = [AgnoMessage(role="user", content=user_msg)]
        if assistant_msg:
            messages.append(AgnoMessage(role="assistant", content=assistant_msg))

        user_id = self._user_id_ref()
        session_id = self._session_id_ref()

        async def _run() -> None:
            try:
                await learning.aprocess(
                    messages=messages,
                    user_id=user_id,
                    session_id=session_id,
                )
            except Exception as exc:  # noqa: BLE001 — opportunistic path
                logger.warning("Learning extraction failed: %s", exc)

        asyncio.create_task(_run())

"""Learning integration — creates an Agno LearningMachine from config.

Exposes :func:`create_learning_machine` as the module's public entry
point and :class:`LearnBootResult` as the typed envelope callers
unwrap. Kept as a free function (not folded into
:class:`~ember_code.core.session.learning_ops.SessionLearningManager`)
because the module-path
``ember_code.core.session.core.create_learning_machine`` is a pinned
test-patch target across the session + learning-ops modules and
their tests. Rule 6 "OOP is mandatory" §Exceptions clause applies:
stateless leaf, no shared subject, no module-level state.
"""

import logging

from agno.db.base import BaseDb
from agno.learn import LearningMachine
from pydantic import BaseModel, ConfigDict

from ember_code.core.config.settings import Settings

logger = logging.getLogger(__name__)


class LearnBootResult(BaseModel):
    """Typed envelope for :func:`create_learning_machine`.

    Distinguishes "learning intentionally disabled" from "learning
    tried to boot and failed" — callers previously got ``None`` for
    both cases and had no way to surface the reason. Pattern 3 typed
    result: `ok` is the wire flag, `machine` is the payload, and
    `reason` explains any `ok=False` state (empty string when
    ``ok=True``).

    ``arbitrary_types_allowed`` is set because Agno's
    :class:`~agno.learn.LearningMachine` is not a Pydantic model in
    the pinned agno version.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    ok: bool
    # Typed as ``object`` (not ``LearningMachine``) because Agno's
    # ``LearningMachine`` has ``BaseDb`` / ``AsyncBaseDb`` / ``Model``
    # ForwardRefs on its ``__init__`` that Pydantic v2 tries to
    # resolve when it introspects a field of that type — even with
    # ``arbitrary_types_allowed=True``. ``object`` accepts any runtime
    # value including the ``None`` sentinel and side-steps the
    # cross-module forward-ref chain.
    machine: object = None
    reason: str = ""


def create_learning_machine(settings: Settings, db: BaseDb | None = None) -> LearnBootResult:
    """Create an Agno LearningMachine if learning is enabled.

    The LearningMachine uses the same db as the session so all
    learning data is co-located with session history and memories.

    Returns a :class:`LearnBootResult` envelope:

    * ``ok=True, machine=<LearningMachine>`` on success.
    * ``ok=False, machine=None, reason=<why>`` when learning is
      disabled, no db is configured, or construction raised. The
      reason string is surfaced by
      :class:`~ember_code.core.session.learning_ops.SessionLearningManager`
      at a higher level with actionable context, rather than buried
      here.
    """
    if not settings.learning.enabled:
        return LearnBootResult(ok=False, machine=None, reason="disabled in settings")

    if db is None:
        return LearnBootResult(ok=False, machine=None, reason="no database configured")

    try:
        lm = LearningMachine(
            db=db,
            user_profile=settings.learning.user_profile,
            user_memory=settings.learning.to_user_memory_input(),
            session_context=settings.learning.session_context,
            entity_memory=settings.learning.entity_memory,
            learned_knowledge=settings.learning.learned_knowledge,
        )
        logger.info(
            "LearningMachine created (profile=%s, memory=%s/agentic, context=%s, entity=%s)",
            settings.learning.user_profile,
            settings.learning.user_memory,
            settings.learning.session_context,
            settings.learning.entity_memory,
        )
        return LearnBootResult(ok=True, machine=lm, reason="")
    except Exception as e:  # noqa: BLE001 — best-effort boot; caller logs reason
        return LearnBootResult(ok=False, machine=None, reason=f"construction failed: {e}")

"""Typed view models for the ``/memory`` slash command's chat output.

Extracted out of :mod:`ember_code.backend.cmd_memory` — the old
procedural module rendered Learning Machine recall payloads via
five free ``_format_*`` functions dispatched by a module-level
``_STORE_FORMATTERS: dict[str, Callable[[Any], str]]`` dict. That
shape violated Rule 6 (dispatch dict of free functions) and Rule 1
(untyped ``dict[str, Any]`` payload with ``getattr`` /
``isinstance`` chains).

Same naming + purpose pattern as the sibling
:mod:`schemas_codeindex` / :mod:`schemas_history` /
:mod:`schemas_run` modules already in ``backend/``.

Model layout:

* :class:`MemoryEntry` / :class:`EntityEntry` — one-row typed
  wrappers around the ``user_memory`` / ``entity_memory`` payload
  items. Each has a ``from_raw`` classmethod that folds the
  ``isinstance(m, dict) else str(m)`` branch onto the entry class
  (kills the inline branches at the old L56 and L72).
* :class:`LearningStoreSection` — polymorphic base with a
  ``to_markdown`` override per subclass. Renders "" when empty.
* Four concrete subclasses — :class:`UserProfileSection`,
  :class:`UserMemorySection`, :class:`EntityMemorySection`,
  :class:`SessionContextSection` — one per known Learning Machine
  store. Unknown store keys route through
  :class:`FallbackSection` which preserves the old
  ``_format_fallback`` output byte-for-byte.
* :class:`LearningRecall` — top-level container with an
  ``aload(learning, user_id)`` classmethod that wraps
  ``learning.arecall`` (returning an empty recall on any
  exception — Pattern 3) and a ``to_command_result()`` render
  method that joins non-empty sections into the final markdown
  (or returns the "no learnings" info result).
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from ember_code.backend.command_result import CommandResult

logger = logging.getLogger(__name__)


class MemoryEntry(BaseModel):
    """One row from the ``user_memory`` store.

    ``learning.arecall`` returns memories as either a plain string
    or a ``{"content": "..."}`` dict. :meth:`from_raw` folds both
    shapes into a typed :class:`MemoryEntry` so the section
    renderer never touches ``isinstance``.
    """

    content: str = ""

    @classmethod
    def from_raw(cls, raw: object) -> MemoryEntry:
        if isinstance(raw, dict):
            return cls(content=str(raw.get("content", "")))
        return cls(content=str(raw))


class EntityEntry(BaseModel):
    """One row from the ``entity_memory`` store.

    Same polymorphism story as :class:`MemoryEntry` — the store
    payload can be a dict with ``name`` / ``description`` keys
    or a plain string. The renderer treats them uniformly.
    """

    name: str = "?"
    description: str = ""
    raw: str = ""  # populated when the source row wasn't dict-shaped

    @classmethod
    def from_raw(cls, raw: object) -> EntityEntry:
        if isinstance(raw, dict):
            return cls(
                name=str(raw.get("name", "?")),
                description=str(raw.get("description", "")),
            )
        return cls(raw=str(raw))

    def to_line(self) -> str:
        if self.raw:
            return f"- {self.raw}\n"
        return f"- **{self.name}**: {self.description}\n"


class LearningStoreSection(BaseModel):
    """Abstract base for one Learning Machine store's rendered
    markdown section.

    Concrete subclasses override :meth:`to_markdown` to emit the
    body lines. :meth:`render` wraps the body with the ``## Title``
    heading and collapses empty bodies to the empty string —
    replacing the old ``_format_learning_section`` free function.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    title: str = ""

    def to_markdown(self) -> str:
        """Return the body markdown (subclass override). Default
        implementation returns "" so an unset section renders
        to nothing."""
        return ""

    def render(self) -> str:
        body = self.to_markdown()
        if not body.strip():
            return ""
        return f"## {self.title}\n{body}"


class UserProfileSection(LearningStoreSection):
    """Renders the ``user_profile`` store — one bullet per set
    profile attribute (name / preferred_name / role / expertise
    / preferences).

    Attributes are optional strings; only truthy ones emit a
    bullet. Mirrors the old ``_format_user_profile`` output
    byte-for-byte.
    """

    title: str = "User Profile"
    name: str | None = None
    preferred_name: str | None = None
    role: str | None = None
    expertise: str | None = None
    preferences: str | None = None

    # Attribute name → bullet label. Kept as a class-level tuple
    # so the rendering order matches the old free function.
    _ATTR_ORDER: ClassVar[tuple[str, ...]] = (
        "name",
        "preferred_name",
        "role",
        "expertise",
        "preferences",
    )

    @classmethod
    def from_store(cls, store_data: Any) -> UserProfileSection:
        return cls(
            name=getattr(store_data, "name", None),
            preferred_name=getattr(store_data, "preferred_name", None),
            role=getattr(store_data, "role", None),
            expertise=getattr(store_data, "expertise", None),
            preferences=getattr(store_data, "preferences", None),
        )

    def to_markdown(self) -> str:
        lines = ""
        for attr in self._ATTR_ORDER:
            val = getattr(self, attr, None)
            if val:
                lines += f"- **{attr.replace('_', ' ').title()}**: {val}\n"
        return lines


class UserMemorySection(LearningStoreSection):
    """Renders the ``user_memory`` store — one bullet per memory
    with non-empty content."""

    title: str = "User Memory"
    memories: list[MemoryEntry] = Field(default_factory=list)

    @classmethod
    def from_store(cls, store_data: Any) -> UserMemorySection:
        raw_memories = getattr(store_data, "memories", []) or []
        return cls(memories=[MemoryEntry.from_raw(m) for m in raw_memories])

    def to_markdown(self) -> str:
        lines = ""
        for m in self.memories:
            if m.content:
                lines += f"- {m.content}\n"
        return lines


class EntityMemorySection(LearningStoreSection):
    """Renders the ``entity_memory`` store — one bullet per
    entity, handling both dict-shaped and string-shaped rows."""

    title: str = "Entity Memory"
    entities: list[EntityEntry] = Field(default_factory=list)

    @classmethod
    def from_store(cls, store_data: Any) -> EntityMemorySection:
        raw_entities = getattr(store_data, "entities", []) or []
        return cls(entities=[EntityEntry.from_raw(e) for e in raw_entities])

    def to_markdown(self) -> str:
        lines = ""
        for e in self.entities:
            lines += e.to_line()
        return lines


class SessionContextSection(LearningStoreSection):
    """Renders the ``session_context`` store — a single ``summary``
    string if present."""

    title: str = "Session Context"
    summary: str | None = None

    @classmethod
    def from_store(cls, store_data: Any) -> SessionContextSection:
        return cls(summary=getattr(store_data, "summary", None))

    def to_markdown(self) -> str:
        return f"{self.summary}\n" if self.summary else ""


class FallbackSection(LearningStoreSection):
    """Renders an unknown store key — stringifies the payload
    verbatim. Preserves the old ``_format_fallback`` output
    byte-for-byte (``f"{store_data}\\n"``)."""

    payload: str = ""

    @classmethod
    def from_store(cls, store_name: str, store_data: Any) -> FallbackSection:
        return cls(
            title=store_name.replace("_", " ").title(),
            payload=f"{store_data}\n",
        )

    def to_markdown(self) -> str:
        return self.payload


# Known-store name → factory. Polymorphism (each factory returns a
# section subclass that knows how to render itself) — NOT a dispatch
# dict of free formatters. The section instances themselves carry
# the ``to_markdown`` behaviour; this table only picks the right
# subclass to instantiate from the untyped ``arecall`` payload.
_KNOWN_STORE_SECTIONS: dict[str, Any] = {
    "user_profile": UserProfileSection.from_store,
    "user_memory": UserMemorySection.from_store,
    "entity_memory": EntityMemorySection.from_store,
    "session_context": SessionContextSection.from_store,
}


class LearningRecall(BaseModel):
    """Container for one ``learning.arecall`` payload.

    Wraps the untyped ``dict[str, Any]`` that
    ``learning.arecall(user_id=...)`` returns into a typed model
    that owns:

    * :meth:`aload` — classmethod that calls ``arecall`` and
      catches any exception (Pattern 3), so callers don't need
      a ``try/except`` themselves.
    * :meth:`to_command_result` — renders the non-empty sections
      into the ``/memory`` chat output, or returns the "no
      learnings stored" info result when everything is empty.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    sections: list[LearningStoreSection] = Field(default_factory=list)

    EMPTY_MESSAGE: ClassVar[str] = (
        "No learnings stored yet. The agent learns from your conversations automatically."
    )

    @classmethod
    def from_recall_dict(cls, data: dict[str, Any]) -> LearningRecall:
        """Build a :class:`LearningRecall` from the raw ``arecall``
        payload. Unknown store keys route through
        :class:`FallbackSection` so the model doesn't regress the
        old fallback behaviour."""
        sections: list[LearningStoreSection] = []
        for store_name, store_data in data.items():
            if not store_data:
                continue
            factory = _KNOWN_STORE_SECTIONS.get(store_name)
            if factory is not None:
                sections.append(factory(store_data))
            else:
                sections.append(FallbackSection.from_store(store_name, store_data))
        return cls(sections=sections)

    @classmethod
    async def aload(cls, learning: Any, user_id: str) -> LearningRecall:
        """Fetch and wrap the Learning Machine's recall for
        ``user_id``. Returns an empty recall on any exception —
        the old procedural code caught the same broad
        ``Exception`` at recall-call sites; the exception envelope
        now lives on the model so the coordinator stays clean."""
        try:
            # Recall with session_id=None to get cross-session data
            # (user profile, user memory, entity memory).
            data = await learning.arecall(user_id=user_id)
        except Exception as exc:
            logger.debug("Learning Machine recall failed: %s", exc)
            data = {}
        return cls.from_recall_dict(data)

    def to_command_result(self) -> CommandResult:
        rendered = [s.render() for s in self.sections]
        rendered = [r for r in rendered if r]
        if not rendered:
            return CommandResult.info(self.EMPTY_MESSAGE)
        return CommandResult.markdown("\n\n".join(rendered))


__all__ = [
    "EntityEntry",
    "EntityMemorySection",
    "FallbackSection",
    "LearningRecall",
    "LearningStoreSection",
    "MemoryEntry",
    "SessionContextSection",
    "UserMemorySection",
    "UserProfileSection",
]

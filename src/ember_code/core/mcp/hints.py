"""What a tool says about itself, and where igni keeps what it was told.

MCP lets a server describe each tool with four hints
(``ToolAnnotations``): whether it only reads, whether a change it makes
can destroy something, whether calling it twice is the same as once,
and whether it reaches beyond the system it belongs to. igni's own
tools are classified by name in the permission catalog; an extension's
tools are classified by these hints, which every extension declares
for every tool its manifest lets reach the agent.

**The manifest is what counts.** An administrator approved a signed
manifest, hints included, so the hints a permission mode reads are the
manifest's. A server that reports different ones at runtime is told
about it (the extension shows the mismatch) and is not believed.

:class:`ToolHintsRegistry` is where the installer puts them, keyed by
the name the agent calls the tool by (``mcp_ext:acme.a_search``), and
where the permission catalog, the pause handler and Bypass's last
check read them. One per process, because the installer runs once per
process and every session's evaluator asks the same question.
"""

from __future__ import annotations

import threading
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ToolHints(BaseModel):
    """MCP's four tool annotations, every one of them stated.

    MCP makes each optional and gives each a default. igni does not: a
    default is a claim the author never made, and a permission mode
    deciding on a claim nobody made is the failure this exists to
    prevent.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    #: It changes nothing anywhere.
    read_only: bool = Field(alias="readOnlyHint")
    #: A change it makes can delete or overwrite something. Meaningful
    #: only for a tool that is not read-only.
    destructive: bool = Field(alias="destructiveHint")
    #: Calling it again with the same arguments changes nothing more.
    idempotent: bool = Field(alias="idempotentHint")
    #: It reaches beyond its own system — the web, another service,
    #: somebody else's data.
    open_world: bool = Field(alias="openWorldHint")

    @model_validator(mode="after")
    def _a_read_destroys_nothing(self) -> ToolHints:
        if self.read_only and self.destructive:
            raise ValueError(
                "a tool cannot be read-only (readOnlyHint) and destructive "
                "(destructiveHint) at once; a read changes nothing to destroy"
            )
        return self

    @property
    def is_local_read(self) -> bool:
        """It reads, and only from its own system."""
        return self.read_only and not self.open_world

    @property
    def is_contained_write(self) -> bool:
        """It writes, destroys nothing, and stays within its own system."""
        return not self.read_only and not self.destructive and not self.open_world

    def as_annotations(self) -> dict[str, bool]:
        """MCP's spelling, as a manifest or ``ToolAnnotations`` carries it."""
        return self.model_dump(by_alias=True)

    @classmethod
    def from_annotations(cls, annotations: Any) -> ToolHints | None:
        """A server's reported annotations, or ``None`` when it states fewer than four.

        Accepts the MCP SDK's ``ToolAnnotations`` or a dict. A server
        that leaves one out has not said what it is, which is not a
        disagreement with the manifest, only an absence.
        """
        if annotations is None:
            return None
        raw = annotations if isinstance(annotations, dict) else annotations.model_dump()
        values = {key: raw.get(key) for key in cls.wire_names()}
        if any(value is None for value in values.values()):
            return None
        try:
            return cls.model_validate(values)
        except ValueError:
            return None

    @classmethod
    def wire_names(cls) -> tuple[str, ...]:
        return tuple(field.alias or name for name, field in cls.model_fields.items())

    def describe(self) -> str:
        """One line for a person: ``read-only, local, idempotent``."""
        if self.read_only:
            parts = ["read-only"]
        else:
            parts = ["destructive" if self.destructive else "changes things"]
        parts.append("reaches outside" if self.open_world else "local")
        if self.idempotent:
            parts.append("idempotent")
        return ", ".join(parts)


class ToolHintsRegistry:
    """The hints of every extension tool the agent can call, by registered name."""

    _shared: ClassVar[ToolHintsRegistry | None] = None
    _shared_lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_name: dict[str, ToolHints] = {}
        self._by_server: dict[str, set[str]] = {}

    @classmethod
    def shared(cls) -> ToolHintsRegistry:
        """The process's registry: the installer writes it, permission checks read it."""
        with cls._shared_lock:
            if cls._shared is None:
                cls._shared = cls()
            return cls._shared

    def declare(self, server: str, hints: dict[str, ToolHints]) -> None:
        """Replace what ``server``'s tools say about themselves.

        ``hints`` is keyed by registered name. Replacing rather than
        merging, so a reinstall that drops a tool drops its hints too.
        """
        with self._lock:
            self._forget_locked(server)
            self._by_name.update(hints)
            self._by_server[server] = set(hints)

    def forget(self, server: str) -> None:
        with self._lock:
            self._forget_locked(server)

    def _forget_locked(self, server: str) -> None:
        for name in self._by_server.pop(server, set()):
            self._by_name.pop(name, None)

    def get(self, tool_name: str) -> ToolHints | None:
        """The hints of a registered tool, or ``None`` for one igni classifies itself."""
        with self._lock:
            return self._by_name.get(tool_name)


__all__ = ["ToolHints", "ToolHintsRegistry"]

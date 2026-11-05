"""What a tool says it is: MCP's four tool annotations, every one stated.

igni's permission modes decide on them — plan mode runs a read-only
tool, Accept Edits a contained write, Bypass everything — so every tool
an extension serves states all four. MCP makes each optional and gives
each a default; igni does not take a default for an answer, and neither
does this SDK: `ext.tool` requires `hints=`, and `ext.run()` refuses to
start while any tool on `ext.mcp` leaves one out.

The hints that count are the ones in the manifest's `permissions.tools`,
which an administrator approved. These are what the server reports; igni
compares the two and shows a difference in the extensions panel.
"""

from __future__ import annotations

from collections.abc import Iterable

from mcp.types import Tool, ToolAnnotations
from pydantic import BaseModel, ConfigDict, model_validator


class ToolHints(BaseModel):
    """What a tool is, in the four words igni's permission modes read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: It changes nothing anywhere.
    read_only: bool
    #: A change it makes can delete or overwrite something. `False` for a read.
    destructive: bool
    #: Calling it again with the same arguments changes nothing more.
    idempotent: bool
    #: It reaches beyond its own system: the web, another service, somebody else's data.
    open_world: bool

    @model_validator(mode="after")
    def _a_read_destroys_nothing(self) -> ToolHints:
        if self.read_only and self.destructive:
            raise ValueError("a read-only tool cannot also be destructive")
        return self

    def annotations(self) -> ToolAnnotations:
        """MCP's `ToolAnnotations`, as the tool is listed to igni."""
        return ToolAnnotations(
            read_only_hint=self.read_only,
            destructive_hint=self.destructive,
            idempotent_hint=self.idempotent,
            open_world_hint=self.open_world,
        )

    def wire(self) -> dict[str, bool]:
        """The four as MCP spells them on the wire, and a manifest does."""
        return self.annotations().model_dump(by_alias=True, exclude_none=True)


class ToolHintsMissing(Exception):
    """Tools on the server that do not state all four hints."""

    def __init__(self, tools: list[str]) -> None:
        self.tools = tools
        super().__init__(
            "every tool states all four hints (readOnlyHint, destructiveHint, "
            "idempotentHint, openWorldHint) and these do not: "
            f"{', '.join(tools)}. Register them with ext.tool(hints=ToolHints(...)), "
            "or pass annotations=ToolHints(...).annotations() to ext.mcp.add_tool."
        )

    @classmethod
    def check(cls, tools: Iterable[Tool]) -> None:
        """Raise for the tools whose annotations leave a hint unstated."""
        missing = sorted(tool.name for tool in tools if not cls.complete(tool.annotations))
        if missing:
            raise cls(missing)

    @staticmethod
    def complete(annotations: ToolAnnotations | None) -> bool:
        """Whether all four hints are stated."""
        if annotations is None:
            return False
        return None not in (
            annotations.read_only_hint,
            annotations.destructive_hint,
            annotations.idempotent_hint,
            annotations.open_world_hint,
        )


__all__ = ["ToolHints", "ToolHintsMissing"]

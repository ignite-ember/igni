"""What the frontend is told about installed extensions.

Pydantic rather than dicts, because this is a wire shape the web
client reads field by field and ``wire-schema.json`` snapshots.

Deliberately not the manifest. A manifest holds things the frontend
has no business with — the signature, the egress allowlist, the
sidecar image — and sending the whole thing would put every one of
them in a browser's memory and in anything that logs a WebSocket
frame. What goes out is what is needed to draw: who it is, what state
it is in, where its UI lives, and what it contributes.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ember_code.extensions.manifest import MenuContribution, PageContribution
from ember_code.extensions.registry import InstalledExtension


class ExtensionPage(BaseModel):
    """A surface the extension renders, as the sidebar needs it."""

    kind: str
    label: str
    sidebar: bool = False
    entry: str = "index.html"


class ExtensionMenuEntry(BaseModel):
    label: str
    command: str
    desc: str


class ExtensionSummary(BaseModel):
    """One installed extension, as the frontend sees it."""

    id: str
    name: str
    version: str
    state: str
    #: Why it is not READY, and what to do. Empty when it is.
    reason: str = ""
    #: Where its UI is served, or empty when it contributes none.
    origin: str = ""
    #: The RPC methods it was granted, so the host frame can refuse
    #: early. The backend re-checks every one of them; this copy
    #: exists so a refusal is immediate rather than a round trip.
    allow: list[str] = Field(default_factory=list)
    pages: list[ExtensionPage] = Field(default_factory=list)
    menu: list[ExtensionMenuEntry] = Field(default_factory=list)

    @classmethod
    def of(cls, entry: InstalledExtension, origin: str | None) -> ExtensionSummary:
        manifest = entry.manifest
        return cls(
            id=manifest.id,
            name=manifest.display_name,
            version=manifest.version,
            state=entry.state.value,
            reason=entry.reason,
            origin=origin or "",
            allow=list(manifest.permissions.rpc),
            pages=[_page(p) for p in manifest.contributes.pages],
            menu=[_menu(m) for m in manifest.contributes.menu],
        )


def _page(page: PageContribution) -> ExtensionPage:
    return ExtensionPage(kind=page.kind, label=page.label, sidebar=page.sidebar, entry=page.entry)


def _menu(entry: MenuContribution) -> ExtensionMenuEntry:
    return ExtensionMenuEntry(label=entry.label, command=entry.command, desc=entry.desc)


class ExtensionListResult(BaseModel):
    """Every installed extension, in a stable order."""

    extensions: list[ExtensionSummary] = Field(default_factory=list)

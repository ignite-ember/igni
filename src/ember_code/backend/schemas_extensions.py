"""What the frontend is told about installed extensions.

Pydantic rather than dicts, because this is a wire shape the web
client reads field by field and ``wire-schema.json`` snapshots.

Deliberately not the manifest. A manifest holds things the frontend
has no business with — the packaging, the egress allowlist, the
sidecar image — and sending the whole thing would put every one of
them in a browser's memory and in anything that logs a WebSocket
frame. What goes out is what is needed to draw: who it is, what state
it is in, where its UI lives, and what it contributes.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ember_code.extensions.manifest import (
    IndicatorContribution,
    MenuContribution,
    PageContribution,
    Setting,
)
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


class ExtensionIndicator(BaseModel):
    """A status chip the host renders in its own chrome."""

    id: str
    label: str
    #: The tool the host polls for ``{label, tone, detail}``.
    source: str
    #: A page kind to open on click, or empty for a chip that only
    #: reports.
    opens: str = ""
    intervalSeconds: float = 30.0


class ExtensionSetting(BaseModel):
    """One configurable value, as the host's settings surface needs it."""

    key: str
    type: str
    label: str
    default: Any = None
    options: list[str] = Field(default_factory=list)


class ExtensionSwitchedOff(BaseModel):
    """One thing an extension does not get, because it collides with another."""

    #: ``prompt:main.workflow``, ``command:/review``, ``agent:explorer``…
    claim: str
    #: Who decided against it, or that nobody has decided yet.
    reason: str


class ExtensionTool(BaseModel):
    """One tool an extension gives the agent, as the panel lists it."""

    name: str
    #: Its hints, for a person: ``read-only, local, idempotent``.
    hints: str


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
    indicators: list[ExtensionIndicator] = Field(default_factory=list)
    #: What this extension can be configured with, for the host to
    #: render. The shape only — the values are in `settings`.
    settings: list[ExtensionSetting] = Field(default_factory=list)
    #: The configured values, which reach the frame as launch params.
    #: Defaults filled in, undeclared keys dropped.
    values: dict[str, Any] = Field(default_factory=dict)
    #: The host-model objects it was granted (``permissions.host``).
    #: The backend checks every request; this copy is for display.
    host: list[str] = Field(default_factory=list)
    #: Its home on the web — its service's URL, the service's
    #: ``client_uri`` — when one is known. See
    #: :mod:`ember_code.extensions.web_home`.
    web_url: str | None = None
    #: Its own service, as its page reaches it (``bridge.service.url``),
    #: when it has one a page may reach — the origin its frame's CSP
    #: admits. The host passes it to the frame in ``hello``.
    service_url: str | None = None
    #: What it does not get because it collides with another
    #: extension. Empty for almost every extension; when not, the
    #: panel says what and why, because "my prompt section is not
    #: there" is otherwise a mystery.
    switched_off: list[ExtensionSwitchedOff] = Field(default_factory=list)
    #: Its prompt edits that land nowhere — another extension overrides
    #: the prompt, and its override has no section the edit names.
    dropped: list[ExtensionSwitchedOff] = Field(default_factory=list)
    #: What is wrong with it that does not stop it working — a tool
    #: whose server reports other hints than its manifest declares.
    problems: list[str] = Field(default_factory=list)
    #: The tools it gives the agent, and what each says it is — what
    #: the permission modes decide on.
    tools: list[ExtensionTool] = Field(default_factory=list)

    @classmethod
    def of(
        cls,
        entry: InstalledExtension,
        origin: str | None,
        values: dict[str, Any] | None = None,
        web_url: str | None = None,
        service_url: str | None = None,
    ) -> ExtensionSummary:
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
            indicators=[_indicator(i) for i in manifest.contributes.indicators],
            settings=[_setting(s) for s in manifest.contributes.settings],
            values=dict(values or {}),
            host=list(manifest.permissions.host),
            web_url=web_url,
            service_url=service_url,
            switched_off=[
                ExtensionSwitchedOff(claim=off.claim, reason=off.reason)
                for off in entry.switched_off
            ],
            dropped=[
                ExtensionSwitchedOff(claim=off.claim, reason=off.reason) for off in entry.dropped
            ],
            problems=list(entry.problems),
            tools=[
                ExtensionTool(name=tool.name, hints=tool.hints.describe())
                for tool in manifest.permissions.tools
            ],
        )


def _page(page: PageContribution) -> ExtensionPage:
    return ExtensionPage(kind=page.kind, label=page.label, sidebar=page.sidebar, entry=page.entry)


def _menu(entry: MenuContribution) -> ExtensionMenuEntry:
    return ExtensionMenuEntry(label=entry.label, command=entry.command, desc=entry.desc)


class PageTokenReply(BaseModel):
    """``extension_service_token``'s answer: a page's token for its service.

    ``ok: false`` with a sentence when there is none to give — nobody
    signed in, no service — because a page shows that as a state.
    """

    ok: bool
    access_token: str = ""
    #: Seconds it has left. The bridge renews a little before.
    expires_in: int = 0
    service_url: str = ""
    error: str = ""


class ExtensionListResult(BaseModel):
    """Every installed extension, in a stable order."""

    extensions: list[ExtensionSummary] = Field(default_factory=list)


def _setting(setting: Setting) -> ExtensionSetting:
    return ExtensionSetting(
        key=setting.key,
        type=setting.type,
        label=setting.label,
        default=setting.default,
        options=list(setting.options),
    )


def _indicator(indicator: IndicatorContribution) -> ExtensionIndicator:
    return ExtensionIndicator(
        id=indicator.id,
        label=indicator.label,
        source=indicator.source,
        opens=indicator.opens or "",
        intervalSeconds=indicator.intervalSeconds,
    )

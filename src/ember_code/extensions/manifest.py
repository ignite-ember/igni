"""The manifest, as a model rather than a dict.

``docs/extension-manifest.schema.json`` is what an extension author
writes against and what their editor checks. This module is the same
contract on the inside: an administrator installs a manifest, the
registry authorises calls against it, and the router reads it to find
the service. Every one of those wants fields, not ``manifest["permissions"]["rpc"]``.

**Two statements of one contract is a drift risk**, and this
repository has been bitten by that shape repeatedly — a published
stylesheet that lost track of the app's classes, token fallbacks that
stopped matching the theme they copied. So
``tests/test_the_manifest_model_and_its_schema_agree.py`` holds the
two to each other: every field here appears in the schema, every
schema field appears here, and a set of manifests the schema refuses
must also fail to parse. When they disagree, the JSON schema wins —
it is the one an author sees.

Unknown fields are rejected rather than ignored, matching the
schema's ``additionalProperties: false``. A manifest key that is
silently dropped is a permission the author believes they declared.
"""

from __future__ import annotations

import json
import re
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Reverse-DNS, and the namespace for everything the extension
#: contributes. Changing it is a new extension, not a new version.
EXTENSION_ID = re.compile(r"^[a-z][a-z0-9]*(\.[a-z][a-z0-9-]*)+$")

#: Semver, because a range in ``contract`` is compared against it.
SEMVER = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$")

#: A slash command, as the host registers it.
COMMAND = re.compile(r"^/[a-z][a-z0-9-]*$")

#: The spelling ``{{THIS}}`` takes inside a prompt.
ANCHOR = re.compile(r"^[A-Z][A-Z0-9_]*$")

#: ``host`` or ``host:port``. Not a URL: a scheme and a path are not
#: things an egress allowlist can enforce, and accepting one would
#: let an author believe their allowlist was narrower than it is.
EGRESS = re.compile(r"^[a-z0-9.-]+(:\d{1,5})?$")

#: A lowercase, hyphenated name.
SLUG = re.compile(r"^[a-z][a-z0-9-]*$")

#: A settings key, as code reads it.
SETTING_KEY = re.compile(r"^[a-z][a-zA-Z0-9_]*$")

#: An event grant: an exact topic, or a ``*``-suffixed prefix. Same
#: grammar as an RPC grant, for the same reason — this list is read
#: by whoever approves the install.
EVENT_GRANT = re.compile(r"^[a-z][a-z0-9.-]*\*?$")

#: An RPC grant: an exact method, or a ``*``-suffixed prefix. No other
#: pattern, because this list is read by whoever approves the install
#: and ``ext.*.scanner.list`` is not something a person can reason
#: about at three in the morning.
RPC_GRANT = re.compile(r"^[a-z0-9_.]+\*?$")


class Strict(BaseModel):
    """Reject what the schema rejects."""

    model_config = ConfigDict(extra="forbid")


class Trust(StrEnum):
    """Who vouched for this extension.

    ``UNSIGNED`` is a development build. An install refuses it unless
    the deployment has explicitly allowed unsigned extensions, because
    the alternative is that the easiest way to ship is the one with no
    provenance.
    """

    FIRST_PARTY = "first-party"
    PARTNER = "partner"
    UNSIGNED = "unsigned"


class Capability(StrEnum):
    """A kind of infrastructure, never a product.

    A regulated customer often has a sanctioned Postgres you must use,
    and a manifest naming ``postgres`` cannot express "or whatever
    they have".
    """

    GRAPH_DB = "graph-db"
    VECTOR_STORE = "vector-store"
    RELATIONAL_DB = "relational-db"
    OBJECT_STORE = "object-store"
    #: Text in, vectors out. A kind rather than a model name for the
    #: usual reason — a regulated customer runs their own, on their
    #: own hardware — and because two extensions sharing one must
    #: agree on the vector width, which is what its ``constraint``
    #: says. See :mod:`ember_code.extensions.capabilities`.
    EMBEDDER = "embedder"


class ResolveMode(StrEnum):
    """How hard to try when satisfying a requirement."""

    SHARED = "shared"
    DEDICATED = "dedicated"
    #: Use an existing instance and fail at install if there is none —
    #: what a customer's sanctioned database requires, and what you
    #: want when provisioning would breach their change control.
    BIND_ONLY = "bind-only"


class Requirement(Strict):
    capability: Capability
    #: What it means depends on the kind: a semver range over the
    #: major version for the stores, ``dim=<n>`` for an embedder.
    #: The table is in :mod:`ember_code.extensions.capabilities`.
    constraint: str
    mode: ResolveMode = ResolveMode.SHARED


class McpBinding(Strict):
    #: A sidecar speaks MCP over stdio. An endpoint speaks it over
    #: SSE; that is the difference between the packagings, not a
    #: choice made here.
    transport: Literal["stdio"]


class Sidecar(Strict):
    """Packaging A: a signed image igni installs and supervises."""

    #: With a tag, and present in the bundle. Never pulled at install:
    #: an air-gapped deployment has no registry to pull from.
    image: str
    mcp: McpBinding
    requires: list[Requirement] = Field(default_factory=list)


class EndpointAuth(Strict):
    kind: Literal["none", "bearer", "mtls"]
    #: The *name* of a secret grant, never the credential.
    secret: str | None = None


class Endpoint(Strict):
    """Packaging B: a service someone else runs, registered rather
    than installed. Always inside the customer's network."""

    url: str
    auth: EndpointAuth | None = None


class DevCommand(Strict):
    """Packaging C: a command igni runs here, speaking MCP over stdio.

    Development only. The other two packagings each need something an
    author does not have on their laptop — a container registry, or a
    service somebody already deployed — so without this there is no
    way to run an extension while writing it.

    Refused on anything but ``unsigned``: this is an arbitrary
    executable named in a text file, and allowing it on a signed
    manifest would mean a signature attested to something that then
    ran whatever it liked.
    """

    #: argv, not a shell string. A shell string would be parsed by a
    #: shell, and which shell that turns out to be is not something a
    #: manifest should depend on.
    command: list[str] = Field(min_length=1)
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)


class Workspace(StrEnum):
    NONE = "none"
    READ = "read"
    WRITE = "write"


class Permissions(Strict):
    """Declared here, granted by an administrator, enforced by the
    base, denied by default."""

    workspace: Workspace = Workspace.NONE
    workspacePaths: list[str] = Field(default_factory=list)
    egress: list[Annotated[str, Field(pattern=EGRESS.pattern)]] = Field(default_factory=list)
    secrets: list[str] = Field(default_factory=list)
    #: MCP tool names the extension may register **with the agent**.
    tools: list[str] = Field(default_factory=list)
    #: Backend RPC methods the extension's **own UI** may call over
    #: the bridge. A different list, granted by the same person,
    #: enforced somewhere else entirely.
    rpc: list[Annotated[str, Field(pattern=RPC_GRANT.pattern)]] = Field(default_factory=list)
    #: Host events this extension may be told about, from the
    #: published vocabulary. Its *own* events need no grant and
    #: another extension's cannot be granted at all — see
    #: :mod:`ember_code.extensions.events`.
    #:
    #: Granting a topic does not decide the *content*: the two
    #: ``workspace.*`` topics also require ``workspace``, and
    #: ``workspace.files-changed`` is filtered to ``workspacePaths``
    #: before it is sent. See
    #: :class:`~ember_code.backend.host_events.HostEvents`.
    events: list[Annotated[str, Field(pattern=EVENT_GRANT.pattern)]] = Field(default_factory=list)


class PageContribution(Strict):
    kind: str
    label: str
    sidebar: bool = False
    entry: str = "index.html"

    @model_validator(mode="after")
    def _namespaced(self) -> PageContribution:
        if not EXTENSION_ID.match(self.kind):
            raise ValueError(
                f"page kind {self.kind!r} is not namespaced; "
                "it must be reverse-DNS so it cannot collide with a "
                "base page or another extension's"
            )
        return self


class MenuContribution(Strict):
    """An entry in the header tools menu.

    ``menu``, not ``tools``: :attr:`Permissions.tools` already means
    MCP tool names, and one word meaning two things in one file is how
    a manifest gets written wrong.
    """

    label: str
    command: Annotated[str, Field(pattern=COMMAND.pattern)]
    desc: str


class IndicatorContribution(Strict):
    """A status chip in the host's own chrome.

    For a capability whose health somebody needs at a glance without
    opening its page — an index that has gone stale is the case this
    exists for, and `CodeIndexIndicator` is the one already in the
    composer today.

    **Not a frame.** A chip is twenty pixels and an iframe is not
    what twenty pixels costs. The host renders it from a value the
    extension reports, which also puts the judgement in the right
    place: what `PREPARING` means for one capability is not what it
    means for another, and only the capability knows.
    """

    id: Annotated[str, Field(pattern=EXTENSION_ID.pattern)]
    label: str
    #: An MCP tool answering ``{label, tone, detail}``. Polled,
    #: because the bridge has no push — so it should be cheap.
    source: str
    #: A page kind this extension contributes, opened on click.
    opens: str | None = None
    intervalSeconds: float = 30.0


class CommandContribution(Strict):
    command: Annotated[str, Field(pattern=COMMAND.pattern)]
    desc: str


class FragmentSource(StrEnum):
    #: igni calls the named MCP tool and uses what it returns, so the
    #: text can change as the extension's data does.
    TOOL = "tool"
    #: A static file in the bundle.
    FILE = "file"


class PromptFragment(Strict):
    anchor: Annotated[str, Field(pattern=ANCHOR.pattern)]
    #: Required: a fragment that declares only an anchor has no text
    #: and nothing to substitute.
    source: FragmentSource
    from_: str | None = Field(default=None, alias="from")

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Setting(Strict):
    key: Annotated[str, Field(pattern=SETTING_KEY.pattern)]
    type: Literal["string", "number", "boolean", "enum"]
    label: str
    default: Any = None
    options: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _enum_has_options(self) -> Setting:
        if self.type == "enum" and not self.options:
            raise ValueError(f"setting {self.key!r} is an enum with no options")
        return self


class Contributes(Strict):
    pages: list[PageContribution] = Field(default_factory=list)
    menu: list[MenuContribution] = Field(default_factory=list)
    indicators: list[IndicatorContribution] = Field(default_factory=list)
    commands: list[CommandContribution] = Field(default_factory=list)
    promptFragments: list[PromptFragment] = Field(default_factory=list)
    promptVariants: list[Annotated[str, Field(pattern=SLUG.pattern)]] = Field(default_factory=list)
    settings: list[Setting] = Field(default_factory=list)


class ExtensionManifest(Strict):
    """Everything an extension declares about itself."""

    id: Annotated[str, Field(pattern=EXTENSION_ID.pattern)]
    version: Annotated[str, Field(pattern=SEMVER.pattern)]
    #: The range of contract versions this works against — the only
    #: compatibility axis. Never an igni version.
    contract: str
    trust: Trust

    name: str | None = None
    description: str | None = None
    vendor: str | None = None
    signature: str | None = None

    sidecar: Sidecar | None = None
    endpoint: Endpoint | None = None
    dev: DevCommand | None = None

    permissions: Permissions = Field(default_factory=Permissions)
    contributes: Contributes = Field(default_factory=Contributes)

    #: Present in the file so an editor finds the schema; carried so a
    #: round trip does not drop it.
    schema_: str | None = Field(default=None, alias="$schema")

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @model_validator(mode="after")
    def _one_packaging(self) -> ExtensionManifest:
        chosen = [
            name
            for name, value in (
                ("sidecar", self.sidecar),
                ("endpoint", self.endpoint),
                ("dev", self.dev),
            )
            if value is not None
        ]
        if len(chosen) != 1:
            found = ", ".join(chosen) if chosen else "none"
            raise ValueError(
                f"{self.id}: exactly one of 'sidecar', 'endpoint' and 'dev' is "
                f"required and {found} was given — packaging is a property of "
                "the extension, not a runtime choice"
            )
        return self

    @model_validator(mode="after")
    def _dev_is_unsigned(self) -> ExtensionManifest:
        if self.dev is not None and self.trust is not Trust.UNSIGNED:
            raise ValueError(
                f"{self.id}: a 'dev' command needs trust 'unsigned', not "
                f"{self.trust.value!r}. It names an arbitrary executable, so a "
                "signature over it would attest to something that then ran "
                "whatever it liked."
            )
        return self

    @model_validator(mode="after")
    def _signed_unless_development(self) -> ExtensionManifest:
        if self.trust is not Trust.UNSIGNED and not self.signature:
            raise ValueError(
                f"{self.id}: trust is {self.trust.value!r} and there is no signature; "
                "anything but a development build carries one"
            )
        return self

    @classmethod
    def load(cls, path: Path) -> ExtensionManifest:
        """Read and validate one ``manifest.json``.

        The path is named in the error, because an install failure
        that says only ``1 validation error`` leaves an administrator
        with a directory of bundles and no idea which.
        """
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}: not valid JSON — {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"{path}: a manifest is a JSON object, got {type(raw).__name__}")
        return cls.model_validate(raw)

    @property
    def display_name(self) -> str:
        """What to show an administrator. Falls back to the id, which
        reads badly and is meant to."""
        return self.name or self.id

    def grants_rpc(self, method: str) -> bool:
        """Whether this extension's UI was granted ``method``.

        Exact names and ``*``-suffixed prefixes, and nothing else. A
        bare ``*`` would grant the whole backend, so it is spelled out
        here rather than falling out of the prefix rule by accident:
        it is only honoured if an administrator literally wrote it.
        """
        for grant in self.permissions.rpc:
            if grant.endswith("*"):
                if method.startswith(grant[:-1]):
                    return True
            elif method == grant:
                return True
        return False

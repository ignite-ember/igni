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

from ember_code.core.mcp.hints import ToolHints
from ember_code.extensions.assets import ExtensionAsset, ExtensionAssets

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

#: A prompt an extension can reach: ``main``, ``plan_mode``, or an agent
#: by the name igni knows it by — ``agent:explorer``, a plugin's
#: ``agent:acme.notes:archivist``. Every one igni ships is in
#: ``docs/prompt-targets.json``, with its sections.
PROMPT_DOCUMENT = re.compile(r"^(main|plan_mode|agent:[a-z0-9][a-z0-9_.:-]*)$")

#: A section of a prompt, by its own name.
SECTION = re.compile(r"^[a-z][a-z0-9_]*$")

#: A settings key, as code reads it.
SETTING_KEY = re.compile(r"^[a-z][a-zA-Z0-9_]*$")

#: A host-model grant: a permission name from the model
#: (``chat.read``), or a ``*``-suffixed prefix (``chat.*``).
HOST_GRANT = re.compile(r"^[a-z][a-z0-9_.]*\*?$")

#: An RPC grant: an exact method, or a ``*``-suffixed prefix. No other
#: pattern, because this list is read by whoever approves the install
#: and ``ext.*.scanner.list`` is not something a person can reason
#: about at three in the morning.
RPC_GRANT = re.compile(r"^[a-z0-9_.]+\*?$")


class Strict(BaseModel):
    """Reject what the schema rejects."""

    model_config = ConfigDict(extra="forbid")


class Trust(StrEnum):
    """Who the author says this extension is from — a label, not trust.

    What makes a bundle trusted is **where it came from**: a bundle igni
    server distributed is trusted because its signature verifies against
    the key of the service a super admin approved (igni server's
    ``docs/service-identity.md`` §8), whatever this label says, and
    anything else is a development install that needs
    ``IGNI_DEV_EXTENSIONS``. A manifest cannot sign itself — the
    signature is over the archive the manifest is inside — so it no
    longer carries one.

    The one rule left on it: ``dev`` packaging requires ``UNSIGNED``,
    because an argv line out of a text file is a development build by
    definition.
    """

    FIRST_PARTY = "first-party"
    PARTNER = "partner"
    UNSIGNED = "unsigned"


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


class EndpointAuth(Strict):
    kind: Literal["none", "bearer", "mtls"]
    #: The *name* of a secret grant, never the credential.
    secret: str | None = None


class Endpoint(Strict):
    """Packaging B: a service someone else runs, registered rather
    than installed. Always inside the customer's network."""

    url: str
    auth: EndpointAuth | None = None


class PythonPackage(Strict):
    """Packaging D: a Python package igni installs and runs here.

    How a service's extension reaches people's machines
    (igni server's ``docs/service-identity.md`` §8). The bundle carries the package
    and every dependency as wheels, so igni installs it into a private
    environment **from the wheelhouse only** — never an index, because
    an air-gapped deployment has none — and starts ``entry`` speaking
    MCP over stdio, as it starts a ``dev`` command.

    A development install runs the same manifest from a source
    checkout: with no wheelhouse in the directory, the package is
    built from the directory itself, network allowed, behind
    ``IGNI_DEV_EXTENSIONS``.
    """

    #: The distribution to install, as pip names it.
    package: str = Field(min_length=1)
    #: Its console script — what igni runs.
    entry: str = Field(min_length=1)
    #: The wheelhouse, a directory inside the bundle.
    wheels: str = "wheels"
    mcp: McpBinding


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


class ToolGrant(Strict):
    """One tool the agent may call, and what it says about itself.

    ``annotations`` is MCP's ``ToolAnnotations``, with every hint
    required: igni's permission modes decide on them (plan mode lets a
    read-only tool run, Accept Edits a contained write, and so on —
    ``docs/EXTENSIBILITY.md``), so a hint left to MCP's default would
    be a decision made on a claim nobody made.
    """

    name: str = Field(min_length=1)
    annotations: ToolHints

    @property
    def hints(self) -> ToolHints:
        return self.annotations


class Permissions(Strict):
    """Declared here, granted by an administrator, enforced by the
    base, denied by default."""

    workspace: Workspace = Workspace.NONE
    workspacePaths: list[str] = Field(default_factory=list)
    egress: list[Annotated[str, Field(pattern=EGRESS.pattern)]] = Field(default_factory=list)
    secrets: list[str] = Field(default_factory=list)
    #: MCP tools the extension may register **with the agent**, each
    #: with all four of MCP's hints — what every permission mode
    #: decides on. See :class:`ToolGrant`.
    tools: list[ToolGrant] = Field(default_factory=list)
    #: Backend RPC methods the extension's **own UI** may call over
    #: the bridge. A different list, granted by the same person,
    #: enforced somewhere else entirely.
    rpc: list[Annotated[str, Field(pattern=RPC_GRANT.pattern)]] = Field(default_factory=list)
    #: Which agents may call this extension's ``tools``.
    #:
    #: ``all-agents`` is MCP's ordinary behaviour. ``own-agents``
    #: offers them only to agents the plugin this extension carries
    #: ships — for a capability whose tools must belong to one role,
    #: because a capability any agent can grant itself is no rule at
    #: all. igni checks it by where an agent's file lives, so it does
    #: not need to know which extension, or which agent, is meant.
    toolAudience: Literal["all-agents", "own-agents"] = "all-agents"
    #: Objects of the host object model this extension may reach —
    #: ``docs/HOST_OBJECT_MODEL.md``. One name per object (``chat.read``,
    #: ``sessions``, ``ui``…), or a ``*``-suffixed prefix. An object
    #: not granted does not exist for the extension: reads fail,
    #: methods are refused and its signals never arrive.
    #:
    #: ``workspace`` is not listed here: ``workspace`` above grants
    #: ``host.workspace``. Its own state, settings and notifications
    #: need no grant. See :mod:`ember_code.extensions.host.grants`.
    host: list[Annotated[str, Field(pattern=HOST_GRANT.pattern)]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _each_tool_once(self) -> Permissions:
        names = [tool.name for tool in self.tools]
        repeated = sorted({name for name in names if names.count(name) > 1})
        if repeated:
            raise ValueError(f"permissions.tools names {', '.join(repeated)} more than once")
        return self

    def tool_names(self) -> list[str]:
        return [tool.name for tool in self.tools]


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
    exists for.

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


class PromptText(Strict):
    """Text an extension puts in the agent's prompt, and when."""

    #: Where the text comes from: a tool igni calls, or a bundled file.
    source: FragmentSource
    from_: str = Field(alias="from", min_length=1)
    #: A condition the extension reports — ``index-covers-head`` — while
    #: which this holds. Without one it holds whenever the extension is
    #: ready.
    when: Annotated[str, Field(pattern=SLUG.pattern)] | None = None

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class PromptOverrideContribution(PromptText):
    """A whole prompt, written by the extension.

    It is made of named sections, as igni's are, and says which: other
    extensions' edits land on the sections it has, and a super admin
    is shown which of theirs it would leave out.
    """

    target: Annotated[str, Field(pattern=PROMPT_DOCUMENT.pattern)]
    #: Its sections, in order. An agent's ``frontmatter`` is implied.
    sections: list[Annotated[str, Field(pattern=SECTION.pattern)]] = Field(min_length=1)
    #: Takes no other extension's edits: one that edits this prompt
    #: collides with it, and a super admin picks one or the other.
    exclusive: bool = False

    @model_validator(mode="after")
    def _sections_once(self) -> PromptOverrideContribution:
        repeated = sorted({s for s in self.sections if self.sections.count(s) > 1})
        if repeated:
            raise ValueError(f"override of {self.target} names {', '.join(repeated)} twice")
        return self


class PromptEditContribution(PromptText):
    """One change to a prompt: exactly one of ``replace``, ``insert`` or ``append``."""

    target: Annotated[str, Field(pattern=PROMPT_DOCUMENT.pattern)]
    #: The section whose text this replaces.
    replace: Annotated[str, Field(pattern=SECTION.pattern)] | None = None
    #: A new section, named so, placed ``before`` or ``after`` another.
    insert: Annotated[str, Field(pattern=SECTION.pattern)] | None = None
    before: Annotated[str, Field(pattern=SECTION.pattern)] | None = None
    after: Annotated[str, Field(pattern=SECTION.pattern)] | None = None
    #: A new section, named so, at the end.
    append: Annotated[str, Field(pattern=SECTION.pattern)] | None = None

    @model_validator(mode="after")
    def _one_change(self) -> PromptEditContribution:
        chosen = [k for k in ("replace", "insert", "append") if getattr(self, k) is not None]
        if len(chosen) != 1:
            raise ValueError(
                f"an edit of {self.target} is exactly one of 'replace', 'insert' and "
                f"'append'; it says {', '.join(chosen) or 'none'}"
            )
        placed = [k for k in ("before", "after") if getattr(self, k) is not None]
        if self.insert is not None and len(placed) != 1:
            raise ValueError(f"inserting {self.insert} needs exactly one of 'before' and 'after'")
        if self.insert is None and placed:
            raise ValueError(f"'{placed[0]}' places an insert, and this edit is not one")
        return self

    @property
    def anchor(self) -> str | None:
        """The section of the prompt this needs: the one replaced, or the one inserted against."""
        return self.replace or self.before or self.after

    @property
    def added(self) -> str | None:
        """The new section's name, for an insert or an append."""
        return self.insert or self.append


class PromptContribution(Strict):
    """What an extension does to the agent's prompts.

    Full control, on purpose: per prompt — ``main``, ``plan_mode``, any
    agent — an extension overrides it whole, or edits it. Edits from
    several extensions stack; replacing one section, overriding one
    prompt, or an exclusive override against anyone's edits, collides,
    and a super admin decides — :mod:`ember_code.extensions.impact`.
    """

    overrides: list[PromptOverrideContribution] = Field(default_factory=list)
    edits: list[PromptEditContribution] = Field(default_factory=list)

    @model_validator(mode="after")
    def _coherent(self) -> PromptContribution:
        overridden = [o.target for o in self.overrides]
        repeated = sorted({t for t in overridden if overridden.count(t) > 1})
        if repeated:
            raise ValueError(f"the prompt contribution overrides {', '.join(repeated)} twice")
        both = sorted({e.target for e in self.edits} & set(overridden))
        if both:
            raise ValueError(
                f"the prompt contribution both overrides and edits {', '.join(both)}; "
                "an override is already all of it"
            )
        replaced = [(e.target, e.replace) for e in self.edits if e.replace is not None]
        twice = sorted({f"{t}#{s}" for t, s in replaced if replaced.count((t, s)) > 1})
        if twice:
            raise ValueError(f"the prompt contribution replaces {', '.join(twice)} twice")
        added = [(e.target, e.added) for e in self.edits if e.added is not None]
        twice = sorted({f"{t}#{s}" for t, s in added if added.count((t, s)) > 1})
        if twice:
            raise ValueError(f"the prompt contribution adds {', '.join(twice)} twice")
        return self


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
    #: Text at a ``{{ANCHOR}}`` in a prompt the extension wrote — its own
    #: agents, an override, an edit.
    promptFragments: list[PromptFragment] = Field(default_factory=list)
    #: The main prompt, plan mode and any agent: overridden or edited.
    prompt: PromptContribution = Field(default_factory=PromptContribution)
    settings: list[Setting] = Field(default_factory=list)
    #: A directory inside the bundle holding a plugin, installed
    #: with the extension and removed with it.
    #:
    #: **Agents, skills and hooks are not contribution types, and
    #: this is why they do not need to be.** A capability usually
    #: brings agents — a document store has the one agent allowed to
    #: touch it, an index has the specialists that query it. igni already has a way to deliver
    #: those, and adding ``contributes.agents`` beside it would be
    #: the second path this contract says not to build.
    #:
    #: So an extension *wraps* a plugin. (Changing an agent igni already
    #: has is different, and is ``prompt``.) The named directory is a
    #: Claude-Code-shaped plugin — ``.claude-plugin/plugin.json``
    #: plus ``agents/``, ``skills/``, ``hooks/`` — copied into the
    #: plugins directory at install and deleted at uninstall. The
    #: loader that was already there picks it up, and nothing about
    #: agents had to learn what an extension is.
    #:
    #: Relative to the bundle root, and inside it: a plugin from
    #: somewhere else is a plugin you install yourself.
    plugin: str | None = None


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

    sidecar: Sidecar | None = None
    endpoint: Endpoint | None = None
    python: PythonPackage | None = None
    dev: DevCommand | None = None

    permissions: Permissions = Field(default_factory=Permissions)
    contributes: Contributes = Field(default_factory=Contributes)

    #: What it runs that is not Python — a database, a JVM, a model —
    #: pinned by size and digest, shipped beside the bundle so a machine
    #: with no network installs it. See :mod:`ember_code.extensions.assets`.
    assets: list[ExtensionAsset] = Field(default_factory=list)

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
                ("python", self.python),
                ("dev", self.dev),
            )
            if value is not None
        ]
        if len(chosen) != 1:
            found = ", ".join(chosen) if chosen else "none"
            raise ValueError(
                f"{self.id}: exactly one of 'sidecar', 'endpoint', 'python' and "
                f"'dev' is required and {found} was given — packaging is a "
                "property of the extension, not a runtime choice"
            )
        return self

    @model_validator(mode="after")
    def _assets_cover_every_platform(self) -> ExtensionManifest:
        ExtensionAssets.check(self.assets)
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
    def _overrides_are_not_plugin_agents(self) -> ExtensionManifest:
        """An agent override's file is not in the plugin's ``agents/``.

        Everything there loads as the plugin's own agent, under the
        extension's name; an override is the other thing — an agent
        igni has, under igni's name — and one file cannot be both.
        """
        plugin = (self.contributes.plugin or "").strip("/")
        if not plugin:
            return self
        for override in self.contributes.prompt.overrides:
            path = override.from_.strip("/")
            if override.source is FragmentSource.FILE and path.startswith(f"{plugin}/agents/"):
                raise ValueError(
                    f"{self.id}: the override of {override.target} reads {override.from_}, "
                    f"inside {plugin}/agents/ — everything there loads as the plugin's own "
                    f"agent; keep overrides beside it, e.g. {plugin}/overrides/"
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
    def packaging(self) -> str:
        """Which of the four packagings this is, by its manifest key."""
        if self.sidecar is not None:
            return "sidecar"
        if self.endpoint is not None:
            return "endpoint"
        if self.python is not None:
            return "python"
        return "dev"

    @property
    def is_development_command(self) -> bool:
        """Whether this runs a ``dev`` command out of its own manifest.

        igni runs one only as a development install, never one that
        arrived from a service: a service ships a ``python`` package,
        installed from wheels igni checked, not an argv line. The
        ``trust`` label does not enter into it — provenance is the
        distribution's signature, not what the manifest says.
        """
        return self.dev is not None

    @property
    def runs_here(self) -> bool:
        """Whether igni starts this extension as a process of its own.

        What decides which grants can be delivered at all: igni sets
        the environment of a process it starts, and an endpoint is
        somebody else's.
        """
        return self.endpoint is None

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

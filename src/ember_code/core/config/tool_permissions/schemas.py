"""Pydantic schemas + typed models for the tool-permissions subsystem.

Mirrors the sibling ``permissions/schemas.py`` shape: this file owns
the wire models, the polymorphic rule hierarchy, and the value objects
that used to live as anonymous ``dict[str, Any]`` blobs and module-level
dispatch tables inside ``tool_permissions.py``.

Key type map (old → new):

* ``PermissionLevel = Literal["allow","ask","deny"]`` — preserved as a
  ``Literal`` alias for wire compatibility with
  :mod:`ember_code.backend.schemas_hitl` (Pydantic serializes existing
  models to the exact same three lowercase strings).
* Old free-function ``_parse_rule`` / ``_match_rule_args`` /
  ``_args_to_str`` / ``_extract_domain`` collapse onto
  :class:`PermissionRule` and its composed :class:`RuleArgPattern`
  hierarchy. Each pattern kind (bare / exact / domain / path / glob)
  is a subclass with its own :meth:`RuleArgPattern.matches` — the
  old ``if key == 'domain' / elif 'path'`` key-dispatch chain is gone,
  replaced by real polymorphism.
* Old ``_DEFAULTS`` module dict → :class:`ToolPermissionDefaults`
  classvar with a typed ``.for_tool()`` lookup.
* Old ``_SETTINGS_TO_TOOL`` string-keyed dict → :class:`CategoryToToolMap`
  keyed on :class:`PermissionCategory` (from
  :mod:`ember_code.core.config.permissions.schemas`) so
  ``PermissionsConfig`` fan-out reads a typed enum, not a stringly
  ``getattr(cfg, field, None)``.
* Old ``_apply_file`` naked ``except Exception`` → :class:`LoadResult`
  typed failure envelope + :class:`EmberSettingsPermissionsFile` wire
  model (Pattern 7 wire/domain split).
"""

from __future__ import annotations

import fnmatch
import logging
import re
from typing import TYPE_CHECKING, Any, ClassVar, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

from ember_code.core.config.permissions.schemas import PermissionCategory

if TYPE_CHECKING:
    from ember_code.core.config.settings import PermissionsConfig

logger = logging.getLogger(__name__)


# ── PermissionLevel — kept as Literal for wire compat ─────────────
#
# ``backend/schemas_hitl.py`` types ``level: PermissionLevel | None``
# on a Pydantic wire model. Switching to a StrEnum would flow through
# every consumer of that wire schema. The three lowercase strings
# ``"allow" / "ask" / "deny"`` are the wire contract; the sibling
# ``permissions/schemas.PermissionLevel`` StrEnum happens to serialize
# to the same three strings, so a future migration is a one-liner
# once the wire consumers are audited.
PermissionLevel = Literal["allow", "ask", "deny"]

_LEVELS: tuple[PermissionLevel, ...] = ("allow", "ask", "deny")


# ── Tool invocation args (typed replacement for dict[str, Any]) ────


class ToolInvocationArgs(BaseModel):
    """Typed view over the ``dict[str, Any]`` args a tool invocation
    carries.

    Owns the ``_args_to_str`` / ``_extract_domain`` helpers that used
    to be free functions. Consumers pass either a raw ``dict`` (via
    :meth:`from_dict`) or construct directly with keyword arguments.
    The class is deliberately lenient — unknown keys are captured in
    :attr:`extra` so the args passed to fnmatch fallback stay lossless.
    """

    model_config = ConfigDict(extra="allow")

    args: list[str] | None = None
    command: str | None = None
    path: str | None = None
    file_path: str | None = None
    file_name: str | None = None
    url: str | None = None
    query: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> ToolInvocationArgs:
        """Build a :class:`ToolInvocationArgs` from a raw dict, preserving
        unknown keys under Pydantic's ``extra`` bucket. ``None`` / empty
        dict → empty instance (no-arg call)."""
        if not raw:
            return cls()
        return cls.model_validate(raw)

    def as_dict(self) -> dict[str, Any]:
        """Round-trip back to a plain dict — used by the fnmatch
        fallback path that has to enumerate every value."""
        return self.model_dump(exclude_none=True)

    def is_empty(self) -> bool:
        """True when no meaningful args were supplied (bare tool call)."""
        return not self.as_dict()

    def primary_string(self) -> str:
        """The matchable string form of these args — absorbs the old
        module-level ``_args_to_str``.

        Priority order (matches historical behaviour):

        1. ``args`` list joined with spaces (shell tools).
        2. First non-empty value among ``path`` / ``file_path`` /
           ``file_name`` / ``url`` / ``query``.
        3. Fallback: every non-None value joined with spaces.
        """
        if self.args:
            return " ".join(str(a) for a in self.args)
        for value in (self.path, self.file_path, self.file_name, self.url, self.query):
            if value:
                return str(value)
        # Fallback: enumerate the whole payload (including ``extra``)
        # so fnmatch has *something* to bite on.
        payload = self.as_dict()
        if not payload:
            return ""
        return " ".join(str(v) for v in payload.values())

    def domain(self) -> str:
        """Extract the URL netloc — used by ``domain:...`` rules."""
        url = self.url or self.query or ""
        if not url:
            return ""
        try:
            return urlparse(str(url)).netloc
        except (ValueError, TypeError):
            return ""

    def path_string(self) -> str:
        """First non-empty path-like field — used by ``path:...`` rules."""
        for value in (self.path, self.file_path, self.file_name):
            if value:
                return str(value)
        return ""


# ── RuleArgPattern — polymorphic replacement for the key-dispatch chain ─


class RuleArgPattern(BaseModel):
    """Base type for the argument-matching side of a permission rule.

    Polymorphic replacement for the old ``_match_rule_args`` chain of
    ``if key == 'domain' / elif 'path' / else fnmatch``. Each concrete
    subclass owns its own :meth:`matches` — adding a new pattern kind
    means adding a subclass, not editing an if/elif ladder.
    """

    raw: str

    def matches(self, args: ToolInvocationArgs) -> bool:
        """Does this pattern match a tool invocation's args? Subclasses
        override — this base returns ``False`` so an unknown pattern
        kind fails closed (safer than True)."""
        raise NotImplementedError


class BarePattern(RuleArgPattern):
    """``ToolName`` — no arg constraint. Matches any invocation."""

    def matches(self, args: ToolInvocationArgs) -> bool:  # noqa: ARG002
        return True


class DomainPattern(RuleArgPattern):
    """``ToolName(domain:github.com)`` — fnmatches the URL netloc."""

    value: str

    def matches(self, args: ToolInvocationArgs) -> bool:
        return fnmatch.fnmatch(args.domain(), self.value)


class PathPattern(RuleArgPattern):
    """``ToolName(path:src/*)`` — fnmatches the first path-like arg."""

    value: str

    def matches(self, args: ToolInvocationArgs) -> bool:
        return fnmatch.fnmatch(args.path_string(), self.value)


class KeyValueGlobPattern(RuleArgPattern):
    """``ToolName(prefix:something)`` for unknown ``prefix``.

    Falls back to fnmatching the primary args string against a
    space-joined ``prefix value`` (or the raw pattern if the prefix
    already contains whitespace — a defensive echo of the pre-refactor
    behaviour so persisted rules with weird prefixes still round-trip).
    """

    key: str
    value: str

    def matches(self, args: ToolInvocationArgs) -> bool:
        pattern = self.raw if " " in self.key else f"{self.key} {self.value}"
        return fnmatch.fnmatch(args.primary_string(), pattern)


class GlobPattern(RuleArgPattern):
    """``ToolName(git status)`` / ``ToolName(git:*)`` — bare fnmatch
    against the primary args string (shell command / joined ``args``
    list). Also the shape ``build_pattern_rule`` emits when it writes
    ``python3 *`` — see :meth:`ToolInvocation.pattern_rule`."""

    def matches(self, args: ToolInvocationArgs) -> bool:
        return fnmatch.fnmatch(args.primary_string(), self.raw)


class PermissionRule(BaseModel):
    """A single ``Tool`` or ``Tool(pattern)`` permission rule.

    Consolidates the two prior ``PermissionRule`` definitions
    (``tool_permissions.PermissionRule`` — the Pydantic data bag —
    and ``permission_eval.PermissionRule`` — the frozen dataclass):
    the shape here is Pydantic (needed for wire round-trip) and
    exposes the same ``.parse`` / ``.matches`` API the dataclass
    version had, so :mod:`ember_code.core.config.permission_eval`
    can drop its private copy and re-import from here.

    Composition over inheritance: :attr:`arg_pattern` holds a
    :class:`RuleArgPattern` subclass — the polymorphism lives there,
    not on ``PermissionRule`` itself. This keeps the door open for
    future ``list[RuleArgPattern]`` (multi-pattern rules) without
    another class explosion.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    tool_name: str
    arg_pattern: RuleArgPattern
    level: PermissionLevel

    # Public regex used by both this class and callers that need to
    # detect whether a raw string is a valid rule shape at all
    # (e.g. :meth:`ToolPermissions.save_rule`).
    _RULE_RE: ClassVar[re.Pattern[str]] = re.compile(r"^(\w+)(?:\((.+)\))?$")

    @classmethod
    def parse(cls, raw: str, level: PermissionLevel = "ask") -> PermissionRule | None:
        """Parse a rule string into a :class:`PermissionRule`.

        Handles the four shapes documented at the top of
        ``tool_permissions/__init__.py``:

        * ``ToolName``                — :class:`BarePattern`
        * ``ToolName(exact args)``    — :class:`GlobPattern`
        * ``ToolName(prefix:*)``      — :class:`GlobPattern`
        * ``ToolName(key:value)``     — :class:`DomainPattern` /
                                        :class:`PathPattern` /
                                        :class:`KeyValueGlobPattern`
        """
        stripped = raw.strip()
        m = cls._RULE_RE.match(stripped)
        if not m:
            return None
        tool_name = m.group(1)
        arg_raw = m.group(2)
        arg_pattern = cls._pattern_from_raw(arg_raw)
        return cls(tool_name=tool_name, arg_pattern=arg_pattern, level=level)

    @classmethod
    def _pattern_from_raw(cls, raw_pattern: str | None) -> RuleArgPattern:
        """Dispatch to the right :class:`RuleArgPattern` subclass based
        on the raw pattern string. Keeps the polymorphic construction
        in one place so the rest of the system just deals with the
        base class."""
        if not raw_pattern:
            return BarePattern(raw="")
        if ":" in raw_pattern:
            key, value = raw_pattern.split(":", 1)
            if key == "domain":
                return DomainPattern(raw=raw_pattern, value=value)
            if key == "path":
                return PathPattern(raw=raw_pattern, value=value)
            return KeyValueGlobPattern(raw=raw_pattern, key=key, value=value)
        return GlobPattern(raw=raw_pattern)

    def matches(self, tool_name: str, args: ToolInvocationArgs) -> bool:
        """Does this rule apply to a call of ``tool_name`` with
        ``args``? Delegates the argument side to the polymorphic
        :attr:`arg_pattern`."""
        if self.tool_name != tool_name:
            return False
        return self.arg_pattern.matches(args)


# ── Defaults + category mapping ────────────────────────────────────


class ToolPermissionDefaults(BaseModel):
    """Default permission level for each catalog tool name.

    Owns the module-level ``_DEFAULTS`` dict that used to sit at
    file top-level. Encapsulating it in a class means test setups
    can construct alternative defaults without monkey-patching a
    module attribute (Rule 5 fix — no module-level mutable state).
    """

    levels: dict[str, PermissionLevel] = Field(
        default_factory=lambda: {
            "Read": "allow",
            "Glob": "allow",
            "Grep": "allow",
            "LS": "allow",
            "Write": "ask",
            "Edit": "ask",
            "Bash": "ask",
            "BashOutput": "ask",
            "Python": "ask",
            "WebSearch": "allow",
            "WebFetch": "allow",
            "NotebookEdit": "ask",
        }
    )

    def for_tool(self, tool_name: str, fallback: PermissionLevel = "ask") -> PermissionLevel:
        """Return the default level for ``tool_name``, or ``fallback``
        (``"ask"`` — the safe conservative default) if the tool isn't
        in the table."""
        return self.levels.get(tool_name, fallback)

    def as_dict(self) -> dict[str, PermissionLevel]:
        """Snapshot copy for seeding a :class:`ToolPermissions`
        instance's mutable level map."""
        return dict(self.levels)


class CategoryToToolMap(BaseModel):
    """Mapping from :class:`PermissionCategory` to the catalog tool
    names that category governs.

    Replaces the string-keyed ``_SETTINGS_TO_TOOL`` dict inside
    ``ToolPermissions``. :meth:`iter_config_levels` gives callers a
    typed ``(level, tools)`` stream by reading each
    :class:`PermissionsConfig` attribute directly — no
    ``getattr(cfg, field_string, None)`` duck-typed probe.
    """

    canonical: dict[PermissionCategory, list[str]] = Field(
        default_factory=lambda: {
            PermissionCategory.FILE_READ: ["Read", "Glob", "Grep", "LS"],
            PermissionCategory.FILE_WRITE: ["Write", "Edit"],
            PermissionCategory.SHELL_EXECUTE: ["Bash", "BashOutput", "Python"],
        }
    )
    web_search_tools: list[str] = Field(default_factory=lambda: ["WebSearch"])
    web_fetch_tools: list[str] = Field(default_factory=lambda: ["WebFetch"])

    def iter_config_levels(self, cfg: PermissionsConfig) -> list[tuple[str, list[str]]]:
        """Read each level directly off ``cfg`` — no stringly-typed
        ``getattr`` dispatch. Returns ``(level_string, [tool_names])``
        for every category with a level set on ``cfg``.
        """
        pairs: list[tuple[str, list[str]]] = []
        for category, tools in self.canonical.items():
            level = _config_level_for(cfg, category)
            if level is not None:
                pairs.append((level, tools))
        if cfg.web_search is not None:
            pairs.append((cfg.web_search, self.web_search_tools))
        if cfg.web_fetch is not None:
            pairs.append((cfg.web_fetch, self.web_fetch_tools))
        return pairs


def _config_level_for(cfg: PermissionsConfig, category: PermissionCategory) -> str | None:
    """Typed lookup: map a :class:`PermissionCategory` enum member to
    its concrete attribute on :class:`PermissionsConfig`."""
    if category is PermissionCategory.FILE_READ:
        return cfg.file_read
    if category is PermissionCategory.FILE_WRITE:
        return cfg.file_write
    if category is PermissionCategory.SHELL_EXECUTE:
        return cfg.shell_execute
    return None


# ── Wire schema for settings.json ──────────────────────────────────


class EmberSettingsPermissionsFile(BaseModel):
    """Typed wire model for the ``permissions`` block inside
    ``settings.json`` / ``settings.local.json``.

    Pattern 7 wire/domain split — this class owns the on-disk shape
    (three plain ``list[str]`` fields), the loader parses raw JSON
    into an instance of this model, and the store then walks each
    list turning strings into :class:`PermissionRule` objects. If a
    future settings-file version changes shape, the wire model
    evolves without ripping through the domain rule types.
    """

    allow: list[str] = Field(default_factory=list)
    ask: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)

    def rules_by_level(self) -> list[tuple[PermissionLevel, str]]:
        """Yield every ``(level, raw_rule)`` pair the file declares —
        the caller then feeds each raw string through
        :meth:`PermissionRule.parse`."""
        pairs: list[tuple[PermissionLevel, str]] = []
        for level, rules in (
            ("allow", self.allow),
            ("ask", self.ask),
            ("deny", self.deny),
        ):
            for raw in rules:
                pairs.append((level, raw))
        return pairs


class LoadResult(BaseModel):
    """Typed outcome of one settings-file load attempt.

    Replaces the old ``except Exception: logger.warning`` swallow —
    the loader returns a ``LoadResult`` per file whether or not it
    parsed successfully, and the caller decides whether to log,
    surface, or apply.
    """

    path: str
    ok: bool
    reason: str | None = None
    file: EmberSettingsPermissionsFile | None = None

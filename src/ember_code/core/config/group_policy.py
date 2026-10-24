"""Group policy pack — fetched from ember-server, cached on disk, applied as a settings tier."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import BaseModel, model_validator

from ember_code.core.paths import CONFIG_DIR, home_config_dir

if TYPE_CHECKING:
    from ember_code.core.plugins.installer import PluginError, PluginInstaller
else:
    PluginInstaller = Any  # type: ignore[misc,assignment]
    PluginError = Exception

logger = logging.getLogger(__name__)

# Cache TTL: 5 minutes. Group policies don't change often enough to need
# sub-minute freshness; a short TTL keeps stale admin pushes from lasting forever.
_CACHE_TTL_SECONDS = 300

#: kind → (directory under the cache, filename for one entry). The
#: directories are the ones each loader is told to scan; the filenames
#: are the conventions those loaders already expect — a skill is a
#: directory with SKILL.md in it, a workflow is an ES module.
_PLAIN_KINDS: dict[str, tuple[str, str]] = {
    "agents": ("agents", "{name}.md"),
    "skills": ("skills", "{name}/SKILL.md"),
    "commands": ("commands", "{name}.md"),
    "workflows": ("workflows", "{name}.mjs"),
    "rules": ("rules", "{name}.md"),
    "output-styles": ("output-styles", "{name}.md"),
    "tools": ("tools", "{name}.py"),
    "scripts": ("scripts", "{name}.sh"),
}

#: Written with the executable bit set. A hook command runs through
#: ``bash -c``; a script arriving mode 0644 fails with 127, which the
#: hook runner treats as non-blocking — a silent no-op on every matching
#: tool call rather than an error anyone sees.
_EXECUTABLE_KINDS: frozenset[str] = frozenset({"scripts"})

#: What a group's hook writes to reach a script the same group ships.
#: Expanded here, because only this side knows where the pack landed.
GROUP_SCRIPTS_PLACEHOLDER = "{group_scripts}"

#: What a stale file of each kind looks like, for pruning.
_SUFFIX: dict[str, str] = {
    "agents": ".md",
    "commands": ".md",
    "workflows": ".mjs",
    "rules": ".md",
    "output-styles": ".md",
    "tools": ".py",
    "mcps": ".json",
    "scripts": ".sh",
}

# Same split :class:`AgentMarkdownFile` uses, so a model written here
# parses back out of the file the loader reads.
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)", re.DOTALL)


def _with_model(content: str, model: str | None) -> str:
    """Return ``content`` with ``model:`` set in its frontmatter.

    An agent's model is already a frontmatter key the loader honours
    (:attr:`AgentDefinition.model` → :meth:`AgentBuilder._resolve_model`),
    so the pack's per-agent model needs no new plumbing — it just has to
    reach the file the loader reads. A legal group can then send contract
    review to one adapter and case summaries to another.

    Content we cannot parse is returned untouched: it would fail to load
    as an agent anyway, and the loader reports that with the file name.
    """
    if not model:
        return content

    match = _FRONTMATTER_RE.match(content)
    if not match:
        logger.warning("Group agent has no YAML frontmatter; cannot set model %r", model)
        return content
    try:
        frontmatter = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        logger.warning("Group agent has unreadable frontmatter; cannot set model %r", model)
        return content
    if not isinstance(frontmatter, dict):
        return content

    frontmatter["model"] = model
    rendered = yaml.safe_dump(frontmatter, sort_keys=False).strip()
    return f"---\n{rendered}\n---\n\n{match.group(2).strip()}\n"


def _try_parse_json(text: str) -> Any:
    """Parse an entry's content as JSON if possible, else pass through.

    The BE serialises ``mcps`` entries as raw MCP server JSON (matching
    :class:`MCPServerConfig` fields), but some legacy entries store
    JSON-as-string already wrapped in ``{...}``. Both shapes should be
    accepted at load time.
    """
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


class GroupPolicyEntry(BaseModel):
    """One entry from a group pack.

    ``source_url`` / ``source_ref`` / ``source_subdir`` are an optional
    trio that mirrors :class:`MarketplacePluginEntry.resolved_source`.
    When ``kind == "plugins"`` and ``source_url`` is set, the policy
    cache treats the entry as "install this plugin from this git URL"
    (via :class:`PluginInstaller`) rather than "write this YAML file
    to disk". The cache also takes them on other kinds for forward
    compatibility.

    There is no ``extensions`` kind. An extension reaches a machine
    from its service through igni server (its ``docs/service-identity.md``
    §8), never from a group's policy; an entry of that kind is
    ignored with a warning.
    """

    kind: str  # agents | mcps | plugins | settings | hooks | …
    entry_name: str
    content: str
    content_type: str  # markdown | yaml | json
    enabled: bool = True

    # Agents only: the model (usually a LoRA adapter) this one runs
    # against. Written into the materialised file's frontmatter, where
    # the loader already reads it. Null inherits the session default.
    model: str | None = None

    # Plugin install source — same shape as marketplace. All three are
    # optional; if any is set, ``source_url`` must be too (half-filled
    # installs would fail in ``PluginInstaller.install`` anyway).
    source_url: str | None = None
    source_ref: str | None = None
    source_subdir: str | None = None

    @model_validator(mode="after")
    def _source_fields_are_complete(self) -> GroupPolicyEntry:
        """Reject half-filled plugin source specs.

        The ``git-subdir`` flow needs the parent URL; ``ref`` and
        ``subdir`` are meaningless without it. Empty strings are
        treated as absent — wire formats commonly serialize "no
        value" as ``""``, and ``is None`` alone would let an
        ``""`` + ``"main"`` slip past the guard.
        """
        url_present = bool(self.source_url and self.source_url.strip())
        ref_present = bool(self.source_ref and self.source_ref.strip())
        subdir_present = bool(self.source_subdir and self.source_subdir.strip())

        if (ref_present or subdir_present) and not url_present:
            raise ValueError("source_ref / source_subdir require source_url to be set")
        return self


class UnchangedPack:
    """The server's answer when the pack we hold is still current.

    A distinct type rather than ``None`` because the two mean opposite
    things to the caller: ``None`` is "I could not tell you", this is "I
    checked, and you are up to date". Conflating them would make a
    failed request look like a confirmation.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "PACK_UNCHANGED"


PACK_UNCHANGED = UnchangedPack()


class GroupPolicyPack(BaseModel):
    """Everything a group gives its people — fetched from ember-server."""

    group_id: str
    group_name: str
    fetched_at: datetime
    entries: list[GroupPolicyEntry] = []

    # What this group's people get when nothing names a model. Routing
    # is resolved server-side, so this is carried for display and for
    # the frontmatter fallback rather than acted on here.
    default_model: str | None = None

    #: The server's tag for this pack's content. Sent back on the next
    #: poll so an unchanged pack costs a 304 and no payload.
    etag: str | None = None

    # Brand overrides for this group — accent colours, a wordmark, a
    # logo. Group-level, not an override entry: entries are
    # materialised as files for agents to read, and this is UI config
    # for the frontend.
    #
    # Deliberately ``dict`` rather than a typed model. Nothing here
    # interprets it — the backend hands it to the webview, which
    # validates every value against its own allowlist before letting
    # any of it near a stylesheet. A schema here would be a second
    # place to update each time the server learns a new token, and it
    # would not be the place doing the checking.
    theme: dict | None = None

    def to_settings_dict(self) -> dict:
        """Convert to a settings dict for the accumulator.

        Only the 'settings' kind entries are merged as config;
        'agents', 'mcps', and 'plugins' are materialized to disk by
        GroupPolicyCache and picked up by their respective loaders.
        """
        result: dict = {}
        for o in self.entries:
            if o.kind == "settings" and o.enabled:
                try:
                    parsed = json.loads(o.content) if o.content_type == "json" else {}
                    if isinstance(parsed, dict):
                        result = _deep_merge(result, parsed)
                except Exception:
                    pass
        return result

    def agent_entries(self) -> list[GroupPolicyEntry]:
        return [o for o in self.entries if o.kind == "agents" and o.enabled]

    def mcp_entries(self) -> list[GroupPolicyEntry]:
        return [o for o in self.entries if o.kind == "mcps" and o.enabled]

    def plugin_disabled_names(self) -> list[str]:
        return [o.entry_name for o in self.entries if o.kind == "plugins" and not o.enabled]


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


class UnsafeEntryName(ValueError):
    """An entry name that would not stay inside the cache directory."""


def safe_entry_name(entry_name: str) -> str:
    """*entry_name* if it is a plain filename, else raise.

    The server sends these names and the cache turns them into paths:
    ``dir_for(kind) / f"{entry_name}.json"``. ``Path`` happily accepts a
    separator, so an entry named ``../../../../../../tmp/x`` wrote
    ``/tmp/x.json`` — outside the cache, on every machine that synced the
    pack. For ``scripts`` the file is written mode 0700, which makes it
    an executable dropped anywhere the user can write.

    Checked here rather than only on the server because this is the side
    that must not be talked into it: a client can be pointed at a server
    somebody else runs, and "the server validates it" is not a property
    this process can verify. The server validates it too.

    Rejects rather than sanitises. A name that has to be rewritten to be
    safe is not the name the admin thinks they published, and silently
    writing ``....tmp.x`` would leave both sides believing something
    different about what shipped.
    """
    name = (entry_name or "").strip()
    if not name:
        raise UnsafeEntryName("entry name is empty")
    if name in {".", ".."}:
        raise UnsafeEntryName(f"entry name {entry_name!r} is a directory reference")
    if "/" in name or "\\" in name:
        raise UnsafeEntryName(f"entry name {entry_name!r} contains a path separator")
    if "\x00" in name:
        raise UnsafeEntryName(f"entry name {entry_name!r} contains a NUL byte")
    if name.startswith("~"):
        raise UnsafeEntryName(f"entry name {entry_name!r} starts with a home reference")
    # ``Path('C:x')`` is drive-relative on Windows, and a bare drive
    # letter is not a filename anywhere.
    if len(name) >= 2 and name[1] == ":":
        raise UnsafeEntryName(f"entry name {entry_name!r} looks like a drive path")
    return name


class GroupPolicyCache:
    """Materializes a group pack's file-type entries to disk.

    Agents (markdown) → ~/.igni/group-policy/agents/<name>.md
    MCPs (json)       → ~/.igni/group-policy/mcps/<name>.json

    Plugins are special-cased: an entry with ``source_url`` is
    installed via :class:`PluginInstaller` into the new ``group-policy``
    root (``<data_dir>/group-policy/plugins/``, priority 4.5 between
    project-igni and managed-igni). An entry without ``source_url``
    falls back to writing its YAML to the legacy path so old entry
    shapes keep working until they're migrated.

    The cache is invalidated whenever the pack's content hash changes.
    """

    def __init__(
        self,
        cache_dir: Path | None = None,
        installer: PluginInstaller | None = None,
        data_dir: Path | None = None,
    ) -> None:
        self._cache_dir = cache_dir or (Path.home() / CONFIG_DIR / "group-policy")
        # Optional deps for the install path. Tests pass a stub
        # installer; production wires ``PluginInstaller(data_dir=...)``
        # from the same data_dir so install + cache land under the
        # same root and the plugins loader can find them.
        self._installer = installer
        self._data_dir = data_dir or (Path.home() / CONFIG_DIR)

    def dir_for(self, kind: str) -> Path:
        """The directory this kind's entries live in, created on demand."""
        name = _PLAIN_KINDS[kind][0] if kind in _PLAIN_KINDS else kind
        d = self._cache_dir / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def path_for(self, kind: str, entry_name: str) -> Path:
        """Where one entry of this kind is written.

        Raises :class:`UnsafeEntryName` for a name that would leave the
        cache directory — see that function for why the client checks
        this itself rather than trusting the server.
        """
        _, filename = _PLAIN_KINDS[kind]
        safe = safe_entry_name(entry_name)
        path = self.dir_for(kind) / filename.format(name=safe)

        # Belt to the braces above, and it does real work: ``filename``
        # is a template from ``_PLAIN_KINDS``, and ``skills`` is already
        # ``{name}/SKILL.md`` — a legitimate separator the template owns
        # rather than the name. So the property is "under the kind's
        # directory", not "directly in it"; asserting the latter broke
        # every skill, which is how this comment came to be accurate.
        root = self.dir_for(kind).resolve()
        resolved = path.resolve()
        if root != resolved and root not in resolved.parents:
            raise UnsafeEntryName(f"entry name {entry_name!r} would write outside {root}")
        return path

    # Named accessors for the kinds other modules reach for directly.
    @property
    def agents_dir(self) -> Path:
        return self.dir_for("agents")

    @property
    def mcps_dir(self) -> Path:
        return self.dir_for("mcps")

    @property
    def plugins_dir(self) -> Path:
        return self.dir_for("plugins")

    def _content_hash(self, content: str) -> str:
        return hashlib.md5(content.encode()).hexdigest()[:12]

    def _materialize_plugin(self, o: GroupPolicyEntry) -> None:
        """Install or skip-write a single plugin entry.

        If ``o.source_url`` is set, defer to :class:`PluginInstaller`,
        which clones to ``<data_dir>/group-policy/plugins/<name>``.
        On install failure (``GitError`` / network / bad manifest /
        already installed by another group) we log and continue —
        one bad plugin must not abort the rest of the pack.

        If ``source_url`` is empty, fall back to writing the entry's
        YAML to the legacy path. This keeps the old entry shape
        working until admins migrate to source-url form.
        """
        if not o.source_url:
            path = self.plugins_dir / f"{safe_entry_name(o.entry_name)}.yaml"
            path.write_text(o.content, encoding="utf-8")
            return

        if self._installer is None:
            # Cache instantiated without an installer — pack author
            # probably meant to ship a plugin source but the wiring
            # is incomplete. Log loud; don't fail the session.
            logger.warning(
                "Group policy plugin entry %r has source_url but no installer "
                "is configured; skipping install. Pass PluginInstaller(data_dir=...) "
                "when constructing GroupPolicyCache to enable remote sources.",
                o.entry_name,
            )
            return

        try:
            self._installer.install(
                o.source_url,
                ref=o.source_ref,
                subdir=o.source_subdir,
            )
            logger.info(
                "Group policy plugin %r installed from %s",
                o.entry_name,
                o.source_url,
            )
        except PluginError as exc:
            # ``PluginError`` covers manifest-missing, already
            # installed by another group, etc. Treat "already
            # installed" as success — the installer is idempotent
            # at the manifest level, not at the URL level.
            msg = str(exc).lower()
            if "already installed" in msg or "already exists" in msg:
                logger.info(
                    "Group policy plugin %r is already installed from %s; reusing",
                    o.entry_name,
                    o.source_url,
                )
                return
            logger.warning(
                "Group policy plugin %r install failed: %s",
                o.entry_name,
                exc,
            )
        except Exception as exc:
            # GitError from the system git binary, network drop,
            # auth failure — log and move on. We never want a
            # broken upstream plugin to disable the rest of the
            # user's plugins.
            logger.warning(
                "Group policy plugin %r install errored: %s",
                o.entry_name,
                exc,
            )

    def materialize(self, pack: GroupPolicyPack) -> None:
        """Write every file-shaped entry to disk, removing stale ones.

        One directory per kind, each of which a loader is told to scan —
        the same arrangement MCP servers have always had. Plugin entries
        with a ``source_url`` are installed by :class:`PluginInstaller`
        instead of written, and settings are merged as config rather
        than materialised at all.

        Also writes ``pack_meta.json`` for the RPC handler to read back.
        """
        # Every kind that owns files gets a set, including the two the
        # loop below writes through their own branch. Missing one is a
        # KeyError the moment a group ships that kind.
        active: dict[str, set[str]] = {kind: set() for kind in _PLAIN_KINDS}
        active["mcps"] = set()
        active["plugins"] = set()
        hooks: dict[str, list] = {}

        for o in pack.entries:
            if not o.enabled:
                continue

            # One unsafe name must not cost the group its whole policy.
            #
            # ``safe_entry_name`` raises, and every write below goes
            # through it — so without this the first bad entry aborts the
            # materialise and the session comes up with no agents, no
            # hooks and no MCP servers. Skipping the entry keeps the other
            # N working, and the warning names it so an admin can fix the
            # one thing that is wrong.
            try:
                safe_entry_name(o.entry_name)
            except UnsafeEntryName as exc:
                logger.warning(
                    "Group policy entry skipped: %s. A %s entry cannot be written "
                    "under that name, so it has been ignored; the rest of the pack "
                    "was applied.",
                    exc,
                    o.kind,
                )
                continue

            if o.kind in _PLAIN_KINDS:
                self._write_plain(o)
                active[o.kind].add(o.entry_name)
            elif o.kind == "mcps":
                # Wrap the per-server content in the ``{mcpServers: {...}}``
                # envelope :class:`MCPConfigLoader` expects. The server
                # sends one server definition per entry; the loader reads
                # N-server files, so it goes under its entry name.
                path = self.dir_for("mcps") / f"{safe_entry_name(o.entry_name)}.json"
                path.write_text(
                    json.dumps({"mcpServers": {o.entry_name: _try_parse_json(o.content)}}),
                    encoding="utf-8",
                )
                active["mcps"].add(o.entry_name)
            elif o.kind == "hooks":
                self._collect_hook(o, hooks)
            elif o.kind == "plugins":
                self._materialize_plugin(o)
                active["plugins"].add(o.entry_name)
            elif o.kind == "extensions":
                # Withdrawn: extensions come from their services
                # through igni, and a policy is somebody else deciding
                # what runs on this machine. Said, so an admin whose
                # pack still carries one learns why nothing arrives.
                logger.warning(
                    "Group policy entry %r is an extension, and a group policy "
                    "no longer installs extensions — they arrive from their "
                    "services through igni server. Ignored.",
                    o.entry_name,
                )

        self._write_hooks(hooks)
        self._write_settings(pack)
        self._prune(active)

        meta = {
            "group_id": pack.group_id,
            "group_name": pack.group_name,
            "fetched_at": pack.fetched_at.isoformat() if pack.fetched_at else None,
            "entry_count": len(pack.entries),
            "default_model": pack.default_model,
            "etag": pack.etag,
            "theme": pack.theme,
        }
        (self._cache_dir / "pack_meta.json").write_text(json.dumps(meta), encoding="utf-8")

    #: Where the pack's ``settings`` entries are persisted, merged.
    SETTINGS_FILE = "group_settings.json"

    def _write_settings(self, pack: GroupPolicyPack) -> None:
        """Persist the pack's merged ``settings`` entries.

        Every other kind owns files and is written above; ``settings`` fell off
        the end of that if/elif and was never written anywhere. The docstring
        said they are "merged as config rather than materialised at all", which
        is true of the live pack and false across a restart — settings are
        loaded before the backend holds a token, so the only pack a fetcher can
        consult at that moment is one on disk. With nothing on disk there was
        nothing to consult, which is half of why ``GroupPolicyTier`` has never
        run in production.

        Written even when empty, and removed when the pack carries no settings,
        so a group that *withdraws* a policy does not leave the last one in
        force forever.
        """
        path = self._cache_dir / self.SETTINGS_FILE
        merged = pack.to_settings_dict()
        if not merged:
            path.unlink(missing_ok=True)
            return
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(merged, indent=2, sort_keys=True), encoding="utf-8")

    def cached_settings(self) -> dict:
        """The last persisted ``settings`` policy, or ``{}``.

        This is the disk-backed half a ``group_policy_fetcher`` needs: settings
        load runs before any network call is possible, so a fetcher that hits
        the portal cannot answer in time. Unreadable or malformed cache reads
        as no policy — a settings load must not fail because a cache file was
        truncated by a crash.

        A file that is *there* and unusable is logged, and an absent one is
        not. Both return ``{}``, and without the distinction the logs cannot
        tell "this person's group publishes no settings" — the ordinary case —
        from "their policy is on disk and this process could not read it".
        """
        path = self._cache_dir / self.SETTINGS_FILE
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            logger.warning(
                "group-policy settings cache %s is unreadable (%s); no org policy applied",
                path,
                exc,
            )
            return {}
        try:
            loaded = json.loads(raw)
        except ValueError as exc:
            logger.warning(
                "group-policy settings cache %s is malformed (%s); no org policy applied",
                path,
                exc,
            )
            return {}
        if not isinstance(loaded, dict):
            logger.warning(
                "group-policy settings cache %s holds %s, not an object; no org policy applied",
                path,
                type(loaded).__name__,
            )
            return {}
        return loaded

    def cached_pack(self) -> GroupPolicyPack | None:
        """The persisted ``settings`` policy, shaped as a pack, or ``None``.

        :class:`~ember_code.core.config.merge_plan.GroupPolicyTier` takes a
        pack and asks it for :meth:`GroupPolicyPack.to_settings_dict`, so the
        cache hands one back rather than a bare dict — one merge path, used by
        the live pack and the cached one alike.

        Only the settings survive the round trip, and that is the whole
        contract: the file-shaped kinds were already materialised to disk by
        :meth:`materialize` and are read by their own loaders, not by the
        settings merge.

        ``None`` means "no policy to apply" for every reason there is — no
        cache, no settings in the group's pack, a truncated file, metadata
        that will not parse. A caller loading settings cannot act on the
        difference, and must not fail on any of them.
        """
        merged = self.cached_settings()
        if not merged:
            return None

        meta = self.read_pack_meta() or {}
        try:
            return GroupPolicyPack(
                # Identity is for the log line and the FE, not the merge —
                # so a pack whose meta went missing still applies its
                # settings rather than being dropped over a display string.
                group_id=str(meta.get("group_id") or "unknown"),
                group_name=str(meta.get("group_name") or "unknown"),
                fetched_at=self._cached_fetched_at(meta),
                entries=[
                    GroupPolicyEntry(
                        kind="settings",
                        entry_name=self.SETTINGS_FILE,
                        content=json.dumps(merged),
                        content_type="json",
                    )
                ],
            )
        except Exception as exc:  # noqa: BLE001 — a bad cache is not a fatal one
            logger.warning(
                "group-policy settings cache in %s could not be read back as a pack "
                "(%s); no org policy applied",
                self._cache_dir,
                exc,
            )
            return None

    @staticmethod
    def _cached_fetched_at(meta: dict) -> datetime:
        """When the cached pack arrived, or now if the meta cannot say.

        ``fetched_at`` is required by the model and used for display and
        staleness, never by the merge — so an unparseable one falls back to
        the current time instead of costing the group its policy.
        """
        raw = meta.get("fetched_at")
        if isinstance(raw, str):
            try:
                return datetime.fromisoformat(raw)
            except ValueError:
                pass
        return datetime.now(timezone.utc)

    def _write_plain(self, o: GroupPolicyEntry) -> None:
        """One entry, one file, at the path its loader looks in."""
        path = self.path_for(o.kind, o.entry_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        content = _with_model(o.content, o.model) if o.kind == "agents" else o.content
        path.write_text(content, encoding="utf-8")
        if o.kind in _EXECUTABLE_KINDS:
            # 0o700 rather than 0o755: nothing else on the machine needs
            # to run it, and this is code that arrived over the network.
            path.chmod(0o700)

    @property
    def scripts_dir(self) -> Path:
        """Where a hook's ``{group_scripts}`` resolves to."""
        return self.dir_for("scripts")

    def _expand_scripts_placeholder(self, command: str) -> str:
        """Point a hook command at the scripts this pack materialised.

        Only this side knows the directory, and it is per-machine, so the
        server stores a placeholder and the substitution happens here.
        Left untouched when absent, so a command naming an absolute path
        or something baked into the image still works.
        """
        return command.replace(GROUP_SCRIPTS_PLACEHOLDER, str(self.scripts_dir))

    def _collect_hook(self, o: GroupPolicyEntry, hooks: dict[str, list]) -> None:
        """Fold one hook entry into the shared ``hooks.json`` shape.

        Hooks are the one kind that is not a file per entry: they are
        declarations keyed by event, and ember-code reads them from a
        settings-shaped file. An entry carries either a bare declaration
        or a whole ``{"hooks": {...}}`` block; both end up merged into
        one file the loader is pointed at.
        """
        try:
            parsed = json.loads(o.content)
        except (TypeError, ValueError):
            logger.warning("Group hook %r is not valid JSON; skipping", o.entry_name)
            return

        block = parsed.get("hooks") if isinstance(parsed, dict) else None
        if isinstance(block, dict):
            for event, declarations in block.items():
                hooks.setdefault(event, []).extend(
                    declarations if isinstance(declarations, list) else [declarations]
                )
            return

        # A bare declaration has to say which event it is for.
        if isinstance(parsed, dict) and parsed.get("event"):
            event = str(parsed.pop("event"))
            hooks.setdefault(event, []).append(parsed)
            return

        logger.warning(
            "Group hook %r names no event and carries no hooks block; skipping",
            o.entry_name,
        )

    def _write_hooks(self, hooks: dict[str, list]) -> None:
        """Write (or remove) the merged hooks file."""
        path = self.dir_for("hooks") / "settings.json"
        if hooks:
            # Expanded here rather than at each parse site: a hook can
            # arrive as a bare declaration or nested inside a ``hooks``
            # block, and doing it once on the merged result covers both.
            for declarations in hooks.values():
                for declaration in declarations:
                    if isinstance(declaration, dict) and isinstance(
                        declaration.get("command"), str
                    ):
                        declaration["command"] = self._expand_scripts_placeholder(
                            declaration["command"]
                        )
            path.write_text(json.dumps({"hooks": hooks}, indent=2), encoding="utf-8")
        else:
            path.unlink(missing_ok=True)

    def _prune(self, active: dict[str, set[str]]) -> None:
        """Remove what the group no longer ships.

        Plugins installed from a source keep their directory: a re-add
        from the same URL skips the install, and deleting a clone to
        reinstall it moments later is a poor trade.
        """
        for kind, names in active.items():
            directory = self.dir_for(kind)
            if not directory.is_dir():
                continue
            for path in sorted(directory.iterdir()):
                stem = path.name[: -len(_SUFFIX[kind])] if kind in _SUFFIX else path.name
                if kind == "skills":
                    stale = path.is_dir() and path.name not in names
                elif kind == "plugins":
                    stale = path.suffix == ".yaml" and path.stem not in names
                else:
                    stale = path.suffix == _SUFFIX.get(kind, "") and stem not in names
                if not stale:
                    continue
                try:
                    if path.is_dir():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
                except Exception as exc:  # noqa: BLE001 — a leftover file is not fatal
                    logger.debug("Could not remove stale group policy path %s: %s", path, exc)

    def read_pack_meta(self) -> dict | None:
        """Read the pack metadata written by materialize(). Returns None if no pack is cached."""
        meta_path = self._cache_dir / "pack_meta.json"
        if not meta_path.is_file():
            return None
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def clear(self) -> None:
        """Remove the entire cache directory."""
        import shutil

        if self._cache_dir.exists():
            shutil.rmtree(self._cache_dir)

    def _is_stale(self) -> bool:
        """Return True when the cached pack is missing or older than TTL."""
        meta = self.read_pack_meta()
        if not meta or not meta.get("fetched_at"):
            return True
        try:
            fetched_at = datetime.fromisoformat(meta["fetched_at"])
        except (TypeError, ValueError):
            return True
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - fetched_at).total_seconds()
        return age > _CACHE_TTL_SECONDS

    def stored_etag(self) -> str | None:
        """The tag the server gave us for the pack on disk."""
        meta = self.read_pack_meta() or {}
        tag = meta.get("etag")
        return str(tag) if tag else None

    async def _call_fetch(self, fetch: Any, token: str) -> Any:
        """Call the fetcher, handing it our tag when it takes one.

        Tests and older callers pass a one-argument coroutine; the real
        client takes ``(token, etag)``. Trying the richer call first
        keeps both working without every stub having to grow a
        parameter it does not use.
        """
        try:
            return await fetch(token, self.stored_etag())
        except TypeError:
            return await fetch(token)

    def _touch(self) -> None:
        """Mark the cached pack as checked just now."""
        meta = self.read_pack_meta()
        if not meta:
            return
        meta["fetched_at"] = datetime.now(timezone.utc).isoformat()
        (self._cache_dir / "pack_meta.json").write_text(json.dumps(meta), encoding="utf-8")

    async def refresh_if_stale(
        self,
        token: str,
        fetch: Any,
        *,
        force: bool = False,
    ) -> bool:
        """Refresh the on-disk pack if the cached one is missing or stale.

        ``fetch`` is an awaitable callable ``(token) -> GroupPolicyPack | None``
        — passed in (rather than constructing PortalClient here) so the auth
        package can stay above the cache in the import graph.

        ``force`` skips the age check and asks anyway. It does **not**
        skip the ETag: an unchanged pack still comes back as
        :class:`UnchangedPack`, so forcing costs a conditional request
        rather than a download. Used at the start of a session, where a
        developer should pick up an admin's change immediately and the
        event is rare enough to afford the round trip — as distinct from
        a CLI invocation, which the age check exists to keep quiet.

        Returns True only when the cache was actually updated. Returns
        False when the cache was fresh, fetch raised, fetch returned
        None, or materialize failed — caller can treat any False as
        "we did not write a new pack".
        """
        if not token:
            return False
        if not force and not self._is_stale():
            return False
        try:
            pack = await self._call_fetch(fetch, token)
        except Exception as exc:
            # Warning (not debug) so portal outages are visible in the
            # standard log without flipping a flag.
            logger.warning("group-policy fetch failed: %s", exc)
            return False
        if isinstance(pack, UnchangedPack):
            # Nothing to write, but the pack on disk is confirmed
            # current — so stamp it, or every poll would find it stale
            # and ask again.
            self._touch()
            return False
        if pack is None:
            return False
        try:
            self.materialize(pack)
        except Exception as exc:
            logger.warning("group-policy materialize failed: %s", exc)
            return False
        return True


async def refresh(
    token: str,
    fetch: Any,
    *,
    cache_dir: Path | None = None,
    data_dir: Path | None = None,
    installer: Any = None,
    force: bool = False,
) -> bool:
    """One-shot cache refresh — used by AuthController on cold start + login.

    Construct a single GroupPolicyCache (mirroring the production defaults)
    and delegate to :meth:`GroupPolicyCache.refresh_if_stale`. Returns False
    when the cache was fresh and no fetch was attempted.

    ``cache_dir`` wins when both are supplied. Otherwise we derive it from
    ``data_dir`` (``<data_dir>/group-policy``) — the same path
    :meth:`GroupPolicyCache.__init__` would use if ``cache_dir`` were the
    canonical root, so callers can hand in just one argument.

    ``installer`` is forwarded to :class:`GroupPolicyCache` so plugin
    entries with ``source_url`` get git-installed instead of being
    skipped. Optional — when omitted, source-URL plugin entries write a
    warning and fall through (same behavior the cache had before).

    ``force`` asks the server regardless of how fresh the cache is —
    see :meth:`GroupPolicyCache.refresh_if_stale`.
    """
    if cache_dir is None and data_dir is not None:
        cache_dir = Path(data_dir).expanduser() / "group-policy"
    cache = GroupPolicyCache(cache_dir=cache_dir, data_dir=data_dir, installer=installer)
    return await cache.refresh_if_stale(token, fetch, force=force)


def group_policy_dir(data_dir: Any = None) -> Path:
    """Where a pack is cached for this install.

    The writer (:func:`refresh`, called by ``AuthController``) derives this
    from ``settings.storage.data_dir``; the reader below has to land on the
    same directory or it reads an empty one and reports "no policy" for a
    group that has one. So the derivation lives here once, and both sides
    call it.

    A missing or nonsensical ``data_dir`` falls back to
    :func:`~ember_code.core.paths.home_config_dir` — the same directory
    ``storage.data_dir`` itself defaults to, and migration-aware, so an
    install still on ``~/.ember`` is not told it has no policy. Anything that
    is not a path is *ignored* rather than coerced: a caller holding a stub
    settings object should get the default, not ``Path('<MagicMock ...>')``.
    """
    if isinstance(data_dir, (str, Path)) and str(data_dir).strip():
        return Path(data_dir).expanduser() / "group-policy"
    return home_config_dir() / "group-policy"


class CachedSettingsFetcher:
    """The ``group_policy_fetcher`` production hands to ``load_settings``.

    Zero-argument and callable, which is the whole contract
    :class:`~ember_code.core.config.merge_plan.GroupPolicyTier` asks for.

    ## Why this reads disk instead of the portal

    A settings load runs before the process holds a bearer token — the token
    is read out of ``Settings``, and ``AuthController`` is constructed with
    the ``Settings`` this fetcher helped build. A fetcher that called the
    portal would therefore have nothing to authenticate with, and would make
    every start-up wait on a network round trip that can hang.

    So the fetch and the read are deliberately in different places. The fetch
    is ``AuthController._hydrate_group_policy`` — cold start, login, and every
    ``settings.group_policy.poll_seconds`` — which writes
    ``group_settings.json`` through :meth:`GroupPolicyCache._write_settings`.
    The read is here. What that buys, and what it costs:

    * Offline, portal down, token expired, laptop asleep for a week: the last
      policy that *did* arrive is still applied. For a policy, that is the
      behaviour an admin wants — a tightening should not evaporate because the
      network did.
    * A policy published while igni is running reaches ``Settings`` at the
      next start. The poll refreshes the cache within ``poll_seconds``, and
      the file-shaped kinds (agents, hooks, MCP servers) pick it up live; the
      settings tier is the one that waits, because ``Settings`` is built once
      and threaded through the session.
    * A brand-new install applies no policy on its very first run: nothing has
      been cached yet. It applies from the second start onwards.

    ## Degradation

    Never raises, never blocks, never touches the network. No cache dir, no
    group, no settings in the group's pack, a file truncated by a crash,
    metadata that will not parse — all of them are "no policy", and the
    settings load continues on local configuration alone.

    ## Logging

    One line per process, and it says which way it went: ``INFO`` naming the
    group and the keys when a policy applies, ``DEBUG`` with the directory
    when there is none (the ordinary case for everyone outside an org, which
    would be noise at ``INFO``). :meth:`GroupPolicyCache.cached_settings`
    warns separately when a cache file exists and cannot be used, so
    "no policy published" and "policy unreadable" are distinguishable.

    Memoized: the read happens once per fetcher, so a caller that builds one
    per process pays one small read however many tiers or loads consult it.
    """

    def __init__(self, data_dir: Any = None, *, cache_dir: Path | None = None) -> None:
        self._cache_dir = cache_dir if cache_dir is not None else group_policy_dir(data_dir)
        self._cache = GroupPolicyCache(cache_dir=self._cache_dir)
        self._pack: GroupPolicyPack | None = None
        self._read = False

    def __call__(self) -> GroupPolicyPack | None:
        if not self._read:
            self._pack = self._read_once()
            self._read = True
        return self._pack

    def _read_once(self) -> GroupPolicyPack | None:
        """Read the cache and say, once, what was found.

        The broad ``except`` is the point of the class: this runs inside
        ``load_settings``, on the path every session and every CLI invocation
        takes, and there is no failure here worth refusing to start over.
        """
        try:
            pack = self._cache.cached_pack()
        except Exception as exc:  # noqa: BLE001 — settings must load regardless
            logger.warning(
                "group-policy cache in %s could not be read (%s); "
                "settings loaded from local configuration only",
                self._cache_dir,
                exc,
            )
            return None

        if pack is None:
            logger.debug(
                "no cached org group policy in %s; settings loaded from local configuration only",
                self._cache_dir,
            )
            return None

        logger.info(
            "org group policy applied to settings: group %s (%s), settings %s, fetched %s, from %s",
            pack.group_name,
            pack.group_id,
            ", ".join(sorted(pack.to_settings_dict())) or "(none)",
            pack.fetched_at.isoformat(),
            self._cache_dir,
        )
        return pack

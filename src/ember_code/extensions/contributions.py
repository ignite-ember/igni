"""What an extension adds to the agent, beyond tools.

EXTENSIBILITY §1 calls this the whole justification for a contract:
MCP carries tools and carries none of the below, and CodeIndex needs
all of it. Without it an extension was a web panel with a backend — it
could not tell the agent what it knows, which is the difference
between installing a capability and installing a page.

An extension may replace the whole of the agent's prompt, or one of
its sections; add to a slot igni leaves; stand in for one of igni's
agents; fill a ``{{ANCHOR}}``; offer a slash command
(``contributes.prompt``, ``promptFragments``, ``commands``). This
registers all of it into ``core``'s tables, and takes it out again.

## Collisions are decided before anything is registered

Two extensions that claim one thing collide, and a super admin decides
which wins on igni server; the decisions arrive with the distribution
listing and are kept in the store. Every change to what is installed
runs :class:`~ember_code.extensions.impact.ImpactResolver` over all of
it, and only what that allows is registered. A collision nobody has
decided is switched off for everyone in it, and
:meth:`ExtensionContributions.switched_off` says why, for the
extensions panel.

## Following the state, not the install

Contributions are tied to ``READY`` rather than to being installed.
An extension whose service has died must stop teaching the agent
about data it can no longer fetch — otherwise the model is told about
a graph schema, reaches for the tool that reads it, and gets a
refusal it cannot act on.

That is why this subscribes to the registry rather than being called
by the installer: the thing that usually takes an extension down is
the health monitor, and a contribution that only followed installs
would survive the thing it describes.

Collisions, though, are worked out over everything *installed*, ready
or not: which extension's text is in the prompt must not flip each
time the other one's health does.

## Why the text is fetched once

Prompt assembly is synchronous, and a ``source: tool`` text needs an
MCP round trip. So the text is fetched when the extension becomes
``READY`` and cached. It changes when its extension restarts, not when
its data does; something that moves minute to minute does not belong
in a prompt. What *can* change at any moment is whether a ``when``
condition holds, and that the extension reports
(:class:`ExtensionConditionReceiver`).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ember_code.core.contributed_commands import ContributedCommands
from ember_code.core.contributed_commands import (
    contributed_commands as default_commands,
)
from ember_code.core.prompts.contributions import (
    PromptClaim,
    PromptClaimKind,
    PromptContributions,
    prompt_contributions,
)
from ember_code.core.prompts.fragments import PromptFragments
from ember_code.core.prompts.fragments import fragments as default_fragments
from ember_code.extensions.impact import (
    Claim,
    ExtensionImpact,
    ImpactResolver,
    Resolution,
    ResolvedImpact,
    SwitchedOff,
)
from ember_code.extensions.installer import SERVER_PREFIX
from ember_code.extensions.manifest import ExtensionManifest, FragmentSource
from ember_code.extensions.registry import (
    ExtensionRegistry,
    ExtensionState,
    InstalledExtension,
)

logger = logging.getLogger(__name__)

#: How long a fragment's tool call may take. A prompt fragment is
#: nice to have; blocking an install on one that never answers is not.
FRAGMENT_TIMEOUT_S = 15.0


class ExtensionContributions:
    """Folds each READY extension's contributions in, and out."""

    def __init__(
        self,
        *,
        registry: ExtensionRegistry,
        rpc: Any,
        bundles: dict[str, Path] | None = None,
        plugins_dir: Path | None = None,
        resolutions: Callable[[], list[Resolution]] | None = None,
        fragments: PromptFragments | None = None,
        prompts: PromptContributions | None = None,
        commands: ContributedCommands | None = None,
    ) -> None:
        """
        Args:
            rpc: The :class:`~ember_code.extensions.rpc.ExtensionRpc`,
                used to call a ``source: tool`` text's tool.
            bundles: Where each extension was installed from, for
                ``source: file``. An extension missing from here can
                still contribute tool-sourced text.
            plugins_dir: Where an extension's plugin is installed. An
                agent an extension stands in for is read from there,
                so it counts as one of the extension's own agents.
            resolutions: The decisions on collisions, as the store
                holds them now.
        """
        self._registry = registry
        self._rpc = rpc
        self._bundles = bundles if bundles is not None else {}
        self._plugins_dir = plugins_dir
        self._resolutions = resolutions or (lambda: [])
        self._fragments = fragments if fragments is not None else default_fragments
        self._prompts = prompts if prompts is not None else prompt_contributions
        self._commands = commands if commands is not None else default_commands
        self._resolved = ResolvedImpact()
        self._pending: set[asyncio.Task] = set()

    def attach(self) -> None:
        """Start following the registry."""
        self._registry.watch(self._on_change)

    def note_bundle(self, extension_id: str, bundle: Path) -> None:
        """Remember where an extension was installed from."""
        self._bundles[extension_id] = bundle

    # ── Collisions ─────────────────────────────────────────────────

    def switched_off(self, extension_id: str) -> list[SwitchedOff]:
        """What this extension does not get, and why."""
        return list(self._resolved.off.get(extension_id, []))

    def resolve(self) -> ResolvedImpact:
        """Work out, over everything installed, who gets what."""
        impacts = [
            ExtensionImpact.of(entry.manifest)
            for extension_id in self._registry.ids()
            if (entry := self._registry.get(extension_id)) is not None
        ]
        self._resolved = ImpactResolver(impacts, self._resolutions()).enforce()
        for extension_id in self._registry.ids():
            entry = self._registry.get(extension_id)
            if entry is not None:
                entry.switched_off = self.switched_off(extension_id)
        for collision in self._resolved.undecided:
            logger.warning(
                "extensions %s collide over %s and nobody has decided which wins; "
                "it is switched off for all of them",
                ", ".join(collision.extensions),
                collision.claim,
            )
        return self._resolved

    def resolutions_changed(self) -> None:
        """New decisions arrived: apply them to everything ready."""
        self._reapply_if_moved(self._resolved, except_for=None)

    def _reapply_if_moved(self, before: ResolvedImpact, *, except_for: str | None) -> None:
        """Re-resolve; when who gets what moved, fold every ready extension in again.

        What an extension lost goes now, synchronously, so nothing
        decided against it is in a prompt built in between; what it
        gained arrives with the re-fold, which may need a tool call.
        """
        self.resolve()
        if before == self._resolved:
            return
        for extension_id in self._registry.ids():
            entry = self._registry.get(extension_id)
            if extension_id == except_for or entry is None:
                continue
            if entry.state is ExtensionState.READY:
                self._withdraw_live(extension_id)
                self._schedule(entry)

    def _allows(self, extension_id: str, claim: str) -> bool:
        return self._resolved.allows(extension_id, claim)

    # ── Reacting ───────────────────────────────────────────────────

    def _on_change(self, entry: InstalledExtension | None, extension_id: str) -> None:
        # Somebody else's claim may have been freed, or taken.
        self._reapply_if_moved(self._resolved, except_for=extension_id)
        if entry is None:
            self.withdraw(extension_id)
            return
        if entry.state is ExtensionState.READY:
            self._schedule(entry)
        else:
            # Down, not gone: nothing it says reaches the agent while
            # it cannot answer, and a command it cannot run is worse
            # than one never offered.
            self._withdraw_live(extension_id)

    def withdraw(self, extension_id: str) -> None:
        """Uninstalled. Forget it entirely."""
        self._withdraw_live(extension_id)

    def _withdraw_live(self, extension_id: str) -> None:
        self._fragments.remove_owner(extension_id)
        self._prompts.remove_owner(extension_id)
        self._commands.remove_owner(extension_id)

    def _schedule(self, entry: InstalledExtension) -> None:
        """Fold in, off the caller's thread of control.

        The registry announces from inside ``install`` and from inside
        a health probe. Both are already async and neither should
        wait on an MCP round trip to record a state.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop — a test or a synchronous caller. Commands and
            # bundled files need none; tool-sourced text is skipped
            # with a note rather than silently dropped.
            self._apply_commands(entry.manifest)
            logger.debug("extension %s: no event loop to fetch its prompt text on", entry.id)
            return

        task = loop.create_task(self.apply(entry.manifest), name=f"contrib-{entry.id}")
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    # ── Applying ───────────────────────────────────────────────────

    async def apply(self, manifest: ExtensionManifest) -> None:
        """Register everything this extension may contribute."""
        self._apply_commands(manifest)
        await self._apply_fragments(manifest)
        await self._apply_prompt(manifest)

    async def _apply_fragments(self, manifest: ExtensionManifest) -> None:
        self._fragments.remove_owner(manifest.id)
        for fragment in manifest.contributes.promptFragments:
            if not self._allows(manifest.id, Claim.anchor(fragment.anchor)):
                continue
            text = await self._fetch(manifest, fragment.anchor, fragment.source, fragment.from_)
            if text is None:
                continue
            refusal = self._fragments.register(fragment.anchor, manifest.id, _supplier(text))
            if refusal is not None:
                logger.warning("extension %s: %s", manifest.id, refusal)

    async def _apply_prompt(self, manifest: ExtensionManifest) -> None:
        prompt = manifest.contributes.prompt
        claims: list[PromptClaim] = []
        for replacement in prompt.replace:
            if not self._allows(manifest.id, Claim.prompt(replacement.target)):
                continue
            text = await self._fetch(
                manifest, replacement.target, replacement.source, replacement.from_
            )
            if text is not None:
                claims.append(
                    PromptClaim(
                        kind=PromptClaimKind.REPLACE,
                        target=replacement.target,
                        text=text,
                        when=replacement.when,
                    )
                )
        for fill in prompt.slots:
            text = await self._fetch(manifest, fill.slot, fill.source, fill.from_)
            if text is not None:
                claims.append(
                    PromptClaim(
                        kind=PromptClaimKind.SLOT, target=fill.slot, text=text, when=fill.when
                    )
                )
        for agent in prompt.agents:
            if not self._allows(manifest.id, Claim.agent(agent.agent)):
                continue
            path = self._agent_file(manifest, agent.from_)
            if path is not None:
                claims.append(
                    PromptClaim(
                        kind=PromptClaimKind.AGENT,
                        target=agent.agent,
                        path=path,
                        when=agent.when,
                    )
                )
        for refusal in self._prompts.set_owner(manifest.id, manifest.display_name, claims):
            logger.warning("extension %s: %s", manifest.id, refusal)

    def _agent_file(self, manifest: ExtensionManifest, origin: str) -> Path | None:
        """Where the file an agent stand-in is defined by lives.

        In the installed plugin when there is one — so the agent is
        among the extension's own and gets its tools under
        ``toolAudience: own-agents`` — and in the bundle otherwise.
        """
        plugin = (manifest.contributes.plugin or "").strip("/")
        inside = origin.strip("/").removeprefix(f"{plugin}/")
        if self._plugins_dir is not None:
            installed = self._plugins_dir / manifest.id / inside
            if installed.is_file():
                return installed
        bundle = self._bundles.get(manifest.id)
        if bundle is None:
            logger.warning(
                "extension %s: its agent file %s cannot be found — the bundle is not known",
                manifest.id,
                origin,
            )
            return None
        return self._contained(manifest, bundle, origin)

    def _apply_commands(self, manifest: ExtensionManifest) -> None:
        """Offer this extension's slash commands.

        The invoker is a closure over the router rather than a name
        the dispatcher resolves later, so there is nothing for
        ``core`` to import back.
        """
        self._commands.remove_owner(manifest.id)
        for entry in manifest.contributes.commands:
            if not self._allows(manifest.id, Claim.command(entry.command)):
                continue
            bare = entry.command.lstrip("/")
            refusal = self._commands.register(
                bare,
                owner=manifest.id,
                description=entry.desc,
                invoke=_invoker(self._rpc, manifest.id, bare),
            )
            if refusal is not None:
                logger.warning("extension %s: %s", manifest.id, refusal)

    def _contained(self, manifest: ExtensionManifest, bundle: Path, origin: str) -> Path | None:
        path = (bundle / origin).resolve()
        # Contained, because the path is a string from a manifest and
        # `../../etc/passwd` is a thing a manifest can say.
        if not path.is_relative_to(bundle.resolve()):
            logger.warning("extension %s: %s is outside its bundle", manifest.id, origin)
            return None
        return path

    async def _fetch(
        self,
        manifest: ExtensionManifest,
        what: str,
        source: FragmentSource,
        origin: str | None,
    ) -> str | None:
        """The text for one contribution, or ``None`` if it cannot be had."""
        if origin is None:
            logger.warning(
                "extension %s: %s has source %s and no 'from', so there is nothing to read",
                manifest.id,
                what,
                source.value,
            )
            return None

        if source is FragmentSource.FILE:
            bundle = self._bundles.get(manifest.id)
            if bundle is None:
                logger.warning(
                    "extension %s: %s reads a bundled file and this "
                    "extension's bundle is not known",
                    manifest.id,
                    what,
                )
                return None
            path = self._contained(manifest, bundle, origin)
            if path is None:
                return None
            try:
                return path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.warning(
                    "extension %s: %s could not read %s: %s", manifest.id, what, origin, exc
                )
                return None

        method = f"ext.{manifest.id}.{origin}"
        try:
            value = await asyncio.wait_for(
                self._rpc.dispatch(caller=manifest.id, method=method, args={}),
                timeout=FRAGMENT_TIMEOUT_S,
            )
        except Exception as exc:
            # One capability failing to describe itself must not stop
            # it being installed, and must not take out the prompt.
            logger.warning(
                "extension %s: %s could not be filled from %s: %s",
                manifest.id,
                what,
                method,
                exc,
            )
            return None
        return value if isinstance(value, str) else str(value)


class ConditionsNotice(BaseModel):
    """The params of ``notifications/igni/conditions``."""

    model_config = ConfigDict(extra="forbid")

    #: ``{condition: holds}``. Only the ones that changed need be sent.
    conditions: dict[str, bool] = Field(max_length=64)


class ExtensionConditionReceiver:
    """``notifications/igni/conditions``: an extension saying what holds now.

    A ``when`` on a prompt contribution names a condition only the
    extension can judge — "my index covers this commit". Its process
    says so on its own MCP connection when it changes, and the
    connection names the sender, so an extension can only ever speak
    for its own conditions. Nothing polls.
    """

    METHOD = "notifications/igni/conditions"

    def __init__(
        self, *, registry: ExtensionRegistry, prompts: PromptContributions | None = None
    ) -> None:
        self._registry = registry
        self._prompts = prompts if prompts is not None else prompt_contributions

    def __call__(self, server: str, params: dict[str, Any]) -> None:
        """The :class:`~ember_code.core.mcp.notification_tap.McpNotificationRouter` handler."""
        if not server.startswith(SERVER_PREFIX):
            logger.warning("%s from %s, which is not an extension: dropped", self.METHOD, server)
            return
        extension_id = server[len(SERVER_PREFIX) :]
        if self._registry.get(extension_id) is None:
            logger.warning("%s from %s, which is not installed: dropped", self.METHOD, server)
            return
        try:
            notice = ConditionsNotice.model_validate(params)
        except ValidationError as exc:
            logger.warning("%s from %s refused: %s", self.METHOD, extension_id, exc)
            return
        if self._prompts.hold(extension_id, notice.conditions):
            logger.info("extension %s: conditions now %s", extension_id, notice.conditions)


def _supplier(text: str) -> Callable[[], str]:
    """A supplier that returns the text it was built with.

    A named function rather than a default-argument lambda: the
    lambda bound the value correctly and read as though it might not,
    which is the kind of line somebody later "fixes" into a closure
    over the loop variable.
    """

    def supply() -> str:
        return text

    return supply


def _invoker(rpc: Any, extension_id: str, command: str) -> Callable[[str], Awaitable[str]]:
    """What running one contributed command does.

    A named factory rather than a lambda in the loop, for the same
    reason :func:`_supplier` is one: a closure over a loop variable
    is the bug this shape is one edit away from.
    """

    async def invoke(text: str) -> str:
        answer = await rpc.invoke_command(extension_id, command, text)
        return answer if isinstance(answer, str) else str(answer)

    return invoke

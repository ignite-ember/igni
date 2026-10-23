"""What an extension adds to the agent, beyond tools.

EXTENSIBILITY §1 calls this the whole justification for a contract:
MCP carries tools and carries none of the below, and CodeIndex needs
all of it. Both mechanisms existed and neither was reachable from a
manifest — ``PromptFragments`` was registered into only by in-tree
CodeIndex, and the agent loader's variant arguments were never passed
by anybody.

So until now an extension was a web panel with a backend. It could
not tell the agent what it knows, which is the difference between
installing a capability and installing a page.

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

## Why the text is fetched once

``PromptFragments`` suppliers are synchronous — they are called
during prompt assembly, which is not an async context — and a
``source: tool`` fragment needs an MCP round trip. So the text is
fetched when the extension becomes ``READY`` and cached, and the
supplier returns the cache.

The honest consequence: a fragment changes when its extension
restarts, not when its data does. For a graph schema, which is what
this exists for, that is the right granularity. For something that
moves minute to minute it is not, and that is a reason not to put
such a thing in a prompt rather than a reason to make this
asynchronous.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ember_code.core.agents.variants import VariantRegistry
from ember_code.core.agents.variants import variants as default_variants
from ember_code.core.contributed_commands import ContributedCommands
from ember_code.core.contributed_commands import (
    contributed_commands as default_commands,
)
from ember_code.core.prompts.fragments import PromptFragments
from ember_code.core.prompts.fragments import fragments as default_fragments
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
    """Folds each READY extension's prompt contributions in, and out."""

    def __init__(
        self,
        *,
        registry: ExtensionRegistry,
        rpc: Any,
        bundles: dict[str, Path] | None = None,
        fragments: PromptFragments | None = None,
        variants: VariantRegistry | None = None,
        commands: ContributedCommands | None = None,
    ) -> None:
        """
        Args:
            rpc: The :class:`~ember_code.extensions.rpc.ExtensionRpc`,
                used to call a ``source: tool`` fragment's tool.
            bundles: Where each extension was installed from, for
                ``source: file``. An extension missing from here can
                still contribute tool-sourced fragments.
        """
        self._registry = registry
        self._rpc = rpc
        self._bundles = bundles if bundles is not None else {}
        self._fragments = fragments if fragments is not None else default_fragments
        self._variants = variants if variants is not None else default_variants
        self._commands = commands if commands is not None else default_commands
        self._pending: set[asyncio.Task] = set()

    def attach(self) -> None:
        """Start following the registry."""
        self._registry.watch(self._on_change)

    def note_bundle(self, extension_id: str, bundle: Path) -> None:
        """Remember where an extension was installed from."""
        self._bundles[extension_id] = bundle

    # ── Reacting ───────────────────────────────────────────────────

    def _on_change(self, entry: InstalledExtension | None, extension_id: str) -> None:
        if entry is None:
            self.withdraw(extension_id)
            return
        if entry.state is ExtensionState.READY:
            self._schedule(entry)
        else:
            # Down, not gone. Its variants stay *known* so a sibling
            # naming one is still recognised and skipped, and its
            # anchors go so nothing describes a capability that
            # cannot answer.
            self._fragments.remove_owner(extension_id)
            self._variants.deactivate(extension_id)
            # A command whose extension cannot answer is worse than
            # one that was never offered: it is in the completion
            # list, the user picks it, and it fails.
            self._commands.remove_owner(extension_id)

    def withdraw(self, extension_id: str) -> None:
        """Uninstalled. Forget it entirely."""
        self._fragments.remove_owner(extension_id)
        self._variants.remove_owner(extension_id)
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
            # No loop — a test or a synchronous caller. Variants are
            # free; the fragments need one and are skipped with a
            # note rather than silently dropped.
            self._apply_variants(entry.manifest)
            self._apply_commands(entry.manifest)
            if entry.manifest.contributes.promptFragments:
                logger.debug(
                    "extension %s has prompt fragments and there is no event loop to fetch them on",
                    entry.id,
                )
            return

        task = loop.create_task(self.apply(entry.manifest), name=f"contrib-{entry.id}")
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    # ── Applying ───────────────────────────────────────────────────

    async def apply(self, manifest: ExtensionManifest) -> None:
        """Register everything this extension contributes."""
        self._apply_variants(manifest)
        self._apply_commands(manifest)
        for fragment in manifest.contributes.promptFragments:
            text = await self._fetch(manifest, fragment.anchor, fragment.source, fragment.from_)
            if text is None:
                continue
            refusal = self._fragments.register(fragment.anchor, manifest.id, _supplier(text))
            if refusal is not None:
                # Two extensions wanting one anchor. Refused rather
                # than last-write-wins: whichever prompt was loaded
                # last would otherwise decide what the model was told.
                logger.warning("extension %s: %s", manifest.id, refusal)

    def _apply_variants(self, manifest: ExtensionManifest) -> None:
        names = manifest.contributes.promptVariants
        if not names:
            return
        self._variants.declare(manifest.id, names)
        self._variants.activate(manifest.id)

    def _apply_commands(self, manifest: ExtensionManifest) -> None:
        """Offer this extension's slash commands.

        The invoker is a closure over the router rather than a name
        the dispatcher resolves later, so there is nothing for
        ``core`` to import back.
        """
        for entry in manifest.contributes.commands:
            bare = entry.command.lstrip("/")
            refusal = self._commands.register(
                bare,
                owner=manifest.id,
                description=entry.desc,
                invoke=_invoker(self._rpc, manifest.id, bare),
            )
            if refusal is not None:
                logger.warning("extension %s: %s", manifest.id, refusal)

    async def _fetch(
        self,
        manifest: ExtensionManifest,
        anchor: str,
        source: FragmentSource,
        origin: str | None,
    ) -> str | None:
        """The text for one anchor, or ``None`` if it cannot be had."""
        if origin is None:
            logger.warning(
                "extension %s: anchor %s has source %s and no 'from', so there is nothing to read",
                manifest.id,
                anchor,
                source.value,
            )
            return None

        if source is FragmentSource.FILE:
            bundle = self._bundles.get(manifest.id)
            if bundle is None:
                logger.warning(
                    "extension %s: anchor %s reads a bundled file and this "
                    "extension's bundle is not known",
                    manifest.id,
                    anchor,
                )
                return None
            path = (bundle / origin).resolve()
            # Contained, because the path is a string from a manifest
            # and `../../etc/passwd` is a thing a manifest can say.
            if not path.is_relative_to(bundle.resolve()):
                logger.warning(
                    "extension %s: anchor %s reads %s, which is outside its bundle",
                    manifest.id,
                    anchor,
                    origin,
                )
                return None
            try:
                return path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.warning(
                    "extension %s: anchor %s could not read %s: %s",
                    manifest.id,
                    anchor,
                    origin,
                    exc,
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
                "extension %s: anchor %s could not be filled from %s: %s",
                manifest.id,
                anchor,
                method,
                exc,
            )
            return None
        return value if isinstance(value, str) else str(value)


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

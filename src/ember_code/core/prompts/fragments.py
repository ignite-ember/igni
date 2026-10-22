"""Named anchors a prompt may contain, and who fills them.

A prompt that teaches an agent about something an extension provides
has to say what that something *is*, and the description belongs with
the extension rather than copied into the prompt. ``{{ANCHOR}}`` marks
where it goes; whoever owns the anchor supplies the text at load time.

This generalises one hardcoded case. ``{{CODEINDEX_GRAPH_SCHEMA}}``
was substituted by a function that imported the schema module
directly, which is exactly the coupling the extension split has to
remove: the base cannot import from a capability that may not be
installed. The anchor is the same; what changed is that the base now
holds a table of them rather than a hardcoded one.

The reason the original was a placeholder at all is worth keeping in
view. The schema was maintained twice — once in the module the
evaluation measured, once by hand inside a shipped agent file — and
the shipped copy knew about none of the facts the score came from
while advertising two properties that were empty on all 86,332
indexed items. One source, substituted in, cannot drift like that. An
anchor whose supplier is an extension cannot drift either, and it can
be absent, which a hardcoded import cannot.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable

logger = logging.getLogger(__name__)

#: What an anchor looks like in a prompt file.
#:
#: Upper snake case between double braces. Deliberately not a template
#: language: a prompt is read by whoever is debugging the agent's
#: behaviour, and every expression it can contain is something they
#: have to evaluate in their head before they know what the model saw.
ANCHOR = re.compile(r"\{\{([A-Z][A-Z0-9_]*)\}\}")


class PromptFragments:
    """Which anchors exist, and what each one expands to.

    One instance is shared by the prompt loader and the agent-body
    loader, so an anchor registered once works in both.
    """

    def __init__(self) -> None:
        self._suppliers: dict[str, tuple[str, Callable[[], str]]] = {}

    def register(self, anchor: str, owner: str, supply: Callable[[], str]) -> str | None:
        """Claim an anchor for ``owner``.

        ``supply`` is called at expansion time rather than now, because
        the text it returns is often expensive to produce and is not
        needed until a prompt that wants it is actually loaded.

        Returns the reason it was refused, or ``None`` when accepted.
        Re-registering the same anchor to the same owner replaces the
        supplier, which is what an extension upgrade does.
        """
        if not ANCHOR.fullmatch("{{" + anchor + "}}"):
            return (
                f"'{anchor}' is not a usable anchor name — upper snake case, starting with a letter"
            )
        held = self._suppliers.get(anchor)
        if held is not None and held[0] != owner:
            return f"'{anchor}' is already supplied by '{held[0]}'"
        self._suppliers[anchor] = (owner, supply)
        return None

    def remove_owner(self, owner: str) -> None:
        """Forget every anchor one extension supplied."""
        for anchor, (by, _) in [*self._suppliers.items()]:
            if by == owner:
                del self._suppliers[anchor]

    def owner_of(self, anchor: str) -> str | None:
        held = self._suppliers.get(anchor)
        return held[0] if held is not None else None

    def anchors(self) -> list[str]:
        return sorted(self._suppliers)

    def expand(self, text: str) -> str:
        """Fill the anchors this table knows. Leave the rest alone.

        **Untouched, not removed.** This is a pass over text that other
        layers also write into: ``{{PERSISTING}}``, ``{{AGENT_CATALOG}}``
        and ``{{CODEINDEX_TOOLS}}`` are filled by the prompt builder
        *after* ``load_prompt`` has run. The first version of this
        dropped every anchor it had no supplier for, on the reasoning
        that a literal brace sequence reaching a model is worse than
        nothing. That is true of the finished prompt and false here —
        this is not the last step, and it deleted three sections of the
        system prompt on the way through. The suite caught it; the
        reasoning was about the wrong moment.

        So the rule is narrow: an anchor with a supplier is replaced, an
        anchor without one is left exactly as it was, and whoever owns
        the next pass decides what it means.

        A **registered** supplier that raises is different. Nothing else
        is going to fill that anchor, so leaving it would put braces in
        front of the model; it is removed and the failure is logged. One
        capability failing to describe itself must not take out every
        prompt in the process.
        """
        if "{{" not in text:
            return text

        def fill(match: re.Match[str]) -> str:
            anchor = match.group(1)
            held = self._suppliers.get(anchor)
            if held is None:
                # Somebody else's placeholder, or nobody's. Not ours to
                # judge, and not ours to delete.
                return match.group(0)
            owner, supply = held
            try:
                return supply()
            except Exception as exc:  # noqa: BLE001 - one anchor, not every prompt
                logger.warning(
                    "prompt anchor %s (from %s) could not be filled; removed: %s",
                    anchor,
                    owner,
                    exc,
                )
                return ""

        return ANCHOR.sub(fill, text)

    @classmethod
    def default(cls) -> PromptFragments:
        """The table a base install starts with.

        A classmethod rather than a module function, matching
        ``ToolSpecCatalog.default`` and ``TipRegistry.default``: the
        thing that knows how to build one of these is the type.

        CodeIndex is registered here while it is still in-tree. When it
        becomes an extension this registers nothing and the extension
        supplies the same anchor under the same name, which is the
        point of naming it rather than importing it.
        """
        fragments = cls()
        fragments.register("CODEINDEX_GRAPH_SCHEMA", "igni.codeindex", cls._codeindex_graph_schema)
        return fragments

    @staticmethod
    def _codeindex_graph_schema() -> str:
        """The live graph description.

        Imported inside the call rather than at module scope because
        the schema lives in the code-index package, which a base
        install need not have. That import is what this whole table
        exists to make optional; it stays until CodeIndex is loaded as
        an extension and registers its own anchor, at which point this
        goes with it.
        """
        from ember_code.core.code_index.neo4j_schema import GRAPH_SCHEMA_DESCRIPTION

        return GRAPH_SCHEMA_DESCRIPTION


#: The table the prompt loaders use.
fragments = PromptFragments.default()

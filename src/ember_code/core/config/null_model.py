"""Null-object placeholder returned when no real model resolves.

Extracted from ``models.py``. Renamed from ``_NoModelConfigured`` to
:class:`NoModelConfigured` (public — the registry hands it back and
:class:`Session` type-checks against it).

Why a subclass of :class:`OpenAILike` and not a bare stand-in:
Session's Agno wiring inside ``_build_main_agent`` type-checks
against the concrete ``OpenAILike`` class (or duck-types on
``ainvoke`` / ``ainvoke_stream``). Preserving the subclass
relationship keeps the null-object story compatible with the Agno
Agent / Team constructors without any type-check bypass.

The error message moves onto the class as a class-level constant
so there's no free module string floating around.
"""

from __future__ import annotations

from agno.models.openai.like import OpenAILike


class NoModelConfigured(OpenAILike):
    """Stand-in model returned when no real model resolves.

    Lets ``Session.__init__`` (and the Agno ``Agent`` / ``Team``
    construction inside ``_build_main_agent``) complete so the TUI
    can render and the user can reach ``/login`` to fix the
    underlying problem (no token, org-membership 403, network down,
    stale credentials, etc.).

    Construction is cheap: ``OpenAILike`` just stores config. Any
    actual model invocation raises the same descriptive
    :class:`ValueError` so the user sees a clear error message in
    chat rather than a network failure from the placeholder URL.
    """

    ERROR_MESSAGE = (
        "No model configured. Run `/login` to discover hosted models from "
        "Ember Cloud, or add a model to `models.registry` in "
        "~/.ember/config.yaml."
    )

    def __init__(self):
        super().__init__(
            id="(no model configured)",
            base_url="https://placeholder.invalid/v1",
            api_key="placeholder",
        )

    @classmethod
    def for_login_required(cls) -> NoModelConfigured:
        """Factory used by ``ModelRegistry.get_model`` when the
        resolution fell all the way through — brand-new install,
        stale cloud token, etc. Kept as a factory so the registry
        never touches construction internals.
        """
        return cls()

    async def ainvoke(self, *_args, **_kwargs):
        raise ValueError(self.ERROR_MESSAGE)

    async def ainvoke_stream(self, *_args, **_kwargs):
        raise ValueError(self.ERROR_MESSAGE)
        yield  # unreachable, satisfies the async-generator typing

    def invoke(self, *_args, **_kwargs):
        raise ValueError(self.ERROR_MESSAGE)

    def invoke_stream(self, *_args, **_kwargs):
        raise ValueError(self.ERROR_MESSAGE)
        yield  # unreachable

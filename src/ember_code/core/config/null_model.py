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

from ember_code.core.paths import DEFAULT_DATA_DIR


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

    # "igni Cloud" used to be the first suggestion here. There is no
    # such service any more — the hosted deployment was shut down and
    # igni is self-hosted — so naming it sent people looking for
    # something that does not exist.
    #
    # `/login` survives the change because it was never cloud-specific:
    # the catalogue client asks whichever server you are signed in to
    # for `/v1/chat/models` (see `cloud_models.py`), which for a
    # self-hoster is their own. It needs a client, though — there is no
    # terminal chat UI — so the config file is given first, as it is the
    # remedy a CLI user can actually reach.
    ERROR_MESSAGE = (
        "No model configured. Add one to `models.registry` in "
        f"{DEFAULT_DATA_DIR}/config.yaml, or run `/login` from a client "
        "to discover the models your igni server offers."
    )

    def __init__(self):
        super().__init__(
            id="(no model configured)",
            base_url="https://placeholder.invalid/v1",
            api_key="placeholder",
        )

    STALE_DEFAULT_MESSAGE = (
        "The configured default model {name!r} is not in `models.registry`. "
        "Run `/login` from a client to rediscover the models your server "
        "offers, or "
        f"pick one with `/model`. Set in {DEFAULT_DATA_DIR}/config.yaml."
    )

    @classmethod
    def for_stale_default(cls, name: str) -> NoModelConfigured:
        """Factory for a default that names a model nobody registers.

        Distinct from :meth:`for_login_required` because the remedy
        differs and because this one is *reachable in production*: a
        stored default is written by ``/model`` and by cloud discovery,
        so removing a model from the deployment strands every developer
        pinned to it. Until this existed that state raised during
        ``Session.__init__`` and the backend died before it could say
        anything at all.
        """
        placeholder = cls()
        placeholder.ERROR_MESSAGE = cls.STALE_DEFAULT_MESSAGE.format(name=name)
        return placeholder

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

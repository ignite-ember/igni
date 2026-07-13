"""Model registry — maps model names to Agno model instances."""

import inspect
import logging
import os
from typing import Any

import httpx
from agno.models.openai.like import OpenAILike

from ember_code.core.auth.credentials import CloudCredentials
from ember_code.core.config.api_keys import resolve_api_key
from ember_code.core.config.model_stream import (
    _aemit_tool_arg_deltas,
    _emit_tool_arg_delta_events,
    _emit_tool_arg_deltas,
    _ToolCallAccumulator,
    _ToolCallAccumulatorStore,
    _ToolCallFragment,
)
from ember_code.core.config.settings import Settings

logger = logging.getLogger(__name__)

# Dedicated LLM call logger — always writes to ~/.ember/llm_calls.log
_llm_logger = logging.getLogger("ember_code.llm_calls")
if not _llm_logger.handlers:
    _llm_log_path = os.path.expanduser("~/.ember/llm_calls.log")
    os.makedirs(os.path.dirname(_llm_log_path), exist_ok=True)
    _llm_handler = logging.FileHandler(_llm_log_path)
    _llm_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))
    _llm_logger.addHandler(_llm_handler)
    _llm_logger.setLevel(logging.INFO)
    # Propagate to root too — so when --debug is on, llm_calls entries
    # also land in ~/.ember/debug.log alongside everything else. Without
    # this, BE-side diagnostics (drain task lifecycle, sub-agent pause
    # surfacing) are silently sent only to llm_calls.log, which makes
    # cross-referencing event flows with the FE timeline impossible.
    _llm_logger.propagate = True

    # Also capture httpx connection lifecycle to diagnose hanging requests
    _httpx_logger = logging.getLogger("httpx")
    _httpx_logger.addHandler(_llm_handler)
    _httpx_logger.setLevel(logging.DEBUG)
    _httpcore_logger = logging.getLogger("httpcore")
    _httpcore_logger.addHandler(_llm_handler)
    _httpcore_logger.setLevel(logging.DEBUG)


_NO_MODEL_ERROR = (
    "No model configured. Run `/login` to discover hosted models from "
    "Ember Cloud, or add a model to `models.registry` in "
    "~/.ember/config.yaml."
)


class _NoModelConfigured(OpenAILike):
    """Stand-in model returned when no real model resolves.

    Lets ``Session.__init__`` (and the Agno ``Agent``/``Team``
    construction inside ``_build_main_agent``) complete so the TUI
    can render and the user can reach ``/login`` to fix the
    underlying problem (no token, org-membership 403, network down,
    stale credentials, etc.). Earlier versions raised at session
    init time, which bricked the binary before any recovery action
    was reachable — restarting was the only "fix" and it didn't
    work because the credential file was the root cause.

    Construction is cheap: ``OpenAILike`` just stores config. Any
    actual model invocation (``ainvoke``, ``ainvoke_stream``,
    ``invoke``, ``aresponse``) raises the same descriptive
    ``ValueError`` so the user sees a clear error message in chat
    rather than a network failure from the dummy endpoint.
    """

    def __init__(self):
        super().__init__(
            id="(no model configured)",
            base_url="https://placeholder.invalid/v1",
            api_key="placeholder",
        )

    async def ainvoke(self, *_args, **_kwargs):
        raise ValueError(_NO_MODEL_ERROR)

    async def ainvoke_stream(self, *_args, **_kwargs):
        raise ValueError(_NO_MODEL_ERROR)
        yield  # unreachable, satisfies the async-generator typing

    def invoke(self, *_args, **_kwargs):
        raise ValueError(_NO_MODEL_ERROR)

    def invoke_stream(self, *_args, **_kwargs):
        raise ValueError(_NO_MODEL_ERROR)
        yield  # unreachable


def _caller_context(depth: int = 4) -> str:
    """Walk the call stack to find the meaningful caller (skip Agno internals)."""
    for frame_info in inspect.stack()[depth : depth + 8]:
        module = frame_info.filename
        if "/agno/" in module or "/openai/" in module or "/httpx/" in module:
            continue
        # Found an ember_code frame
        short = module.rsplit("ember_code/", 1)[-1] if "ember_code/" in module else module
        return f"{short}:{frame_info.lineno} ({frame_info.function})"
    return "unknown"


def _sanitize_messages(messages: list) -> list:
    """Convert multimodal content arrays to plain text.

    When a non-vision model receives messages from a session that
    previously used a vision model, content may be a list of dicts
    (text + image_url + file). This extracts only the text parts.
    """
    for msg in messages:
        content = getattr(msg, "content", None) if not isinstance(msg, dict) else msg.get("content")
        if isinstance(content, list):
            text_parts = []
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        text_parts.append(part.get("text", ""))
                elif isinstance(part, str):
                    text_parts.append(part)
            new_content = "\n".join(text_parts) if text_parts else ""
            if isinstance(msg, dict):
                msg["content"] = new_content
            else:
                msg.content = new_content
    return messages


# Tool-call argument streaming lives in ``model_stream.py`` so this
# file stays focused on the model registry / logging-model pair.
# The imports at module top re-export the streaming symbols on the
# ``ember_code.core.config.models`` import path so existing tests
# and callers keep working.


class _LoggingModel(OpenAILike):
    """Thin wrapper that logs calls, sanitizes messages for non-vision
    models, AND emits ``CustomEvent`` deltas as tool-call arguments
    stream.

    Tool-arg streaming
    ------------------
    Agno's ``_parse_provider_response_delta`` passes ``choice_delta.tool_calls``
    through on every stream chunk, and ``_populate_stream_data`` yields
    a ``ModelResponse`` with those deltas. But the agent-layer
    ``handle_model_response_chunk`` only reads ``.content`` /
    ``.reasoning_content`` from delta chunks and never inspects
    ``.tool_calls`` — so partial tool arguments are silently
    accumulated in ``stream_data.response_tool_calls`` and only
    surface AFTER the whole tool call completes (as
    ``ToolCallStartedEvent``).

    That kills progressive rendering for tools whose value IS the
    argument shape — the visualizer sub-agent's ``visualize({spec:
    {...}})`` call being the driving case. We want the FE to render
    the spec as its tokens land.

    Fix: wrap ``process_response_stream`` /
    ``aprocess_response_stream`` (the polymorphic yield point whose
    return type is ``Iterator[ModelResponse]`` but which is documented
    to accept ``RunOutputEvent`` bubble-up). On each ``ModelResponse``
    that carries tool_call deltas, we ALSO yield a
    ``CustomEvent(event='tool_call_input_delta', ...)`` carrying the
    accumulated arguments string per (tool_call_id | tool_index).
    The FE-facing agent stream then sees the CustomEvent alongside
    the existing ``RunContentEvent`` / ``ToolCallStartedEvent``
    lifecycle. Agno's normal accumulator sees ``ModelResponse``
    untouched — so the tool still executes normally when done.
    """

    _vision: bool = False

    def invoke(self, *args, **kwargs):
        self._log_call("invoke", args, stream=False, kwargs=kwargs)
        if not self._vision and args:
            args = (_sanitize_messages(args[0]), *args[1:])
        return super().invoke(*args, **kwargs)

    async def ainvoke(self, *args, **kwargs):
        self._log_call("ainvoke", args, stream=False, kwargs=kwargs)
        if not self._vision and args:
            args = (_sanitize_messages(args[0]), *args[1:])
        return await super().ainvoke(*args, **kwargs)

    def invoke_stream(self, *args, **kwargs):
        self._log_call("invoke_stream", args, stream=True, kwargs=kwargs)
        if not self._vision and args:
            args = (_sanitize_messages(args[0]), *args[1:])
        yield from super().invoke_stream(*args, **kwargs)

    async def ainvoke_stream(self, *args, **kwargs):
        self._log_call("ainvoke_stream", args, stream=True, kwargs=kwargs)
        if not self._vision and args:
            args = (_sanitize_messages(args[0]), *args[1:])
        async for chunk in super().ainvoke_stream(*args, **kwargs):
            yield chunk

    def process_response_stream(self, *args, **kwargs):
        yield from _emit_tool_arg_deltas(super().process_response_stream(*args, **kwargs))

    async def aprocess_response_stream(self, *args, **kwargs):
        async for ev in _aemit_tool_arg_deltas(super().aprocess_response_stream(*args, **kwargs)):
            yield ev

    def _log_call(self, method: str, args: tuple, stream: bool, kwargs: dict | None = None) -> None:
        n_messages = len(args[0]) if args else len((kwargs or {}).get("messages", []))
        url = getattr(self, "base_url", None) or "default"
        # Build a short stack trace showing ember_code frames
        frames = []
        for fi in inspect.stack()[2:15]:
            mod = fi.filename
            if "/agno/" in mod or "/openai/" in mod or "/httpx/" in mod or "/asyncio/" in mod:
                continue
            short = (
                mod.rsplit("ember_code/", 1)[-1] if "ember_code/" in mod else os.path.basename(mod)
            )
            frames.append(f"{short}:{fi.lineno}({fi.function})")
        caller = " <- ".join(frames[:4]) or "unknown"
        _llm_logger.info(
            "LLM call: %s | model=%s | messages=%d | stream=%s | url=%s | caller=%s",
            method,
            self.id,
            n_messages,
            stream,
            url,
            caller,
        )


# Context-window resolution lives in ``context_window.py`` — this
# file focuses on the model registry / logging-model pair. The
# re-export below preserves the old ``ember_code.core.config.models``
# import path.
from ember_code.core.config.context_window import (  # noqa: E402
    DEFAULT_CONTEXT_WINDOW,
    ContextWindowResolver,
)


class ModelRegistry:
    """Registry that maps model names to Agno model instances.

    All models (including Ember defaults) are defined in the config registry
    (``models.registry``). Built-in defaults ship via ``defaults.py`` and can
    be overridden by user/project config files.

    Resolution order:
    1. Config registry (defaults + user overrides)
    2. ``provider:model_id`` format (e.g., ``openai_like:gpt-4o``)
    """

    PROVIDERS: dict[str, type] = {
        "openai_like": OpenAILike,
    }

    @classmethod
    def _load_provider(cls, name: str) -> type | None:
        """Lazy-load provider classes that require optional dependencies."""
        if name == "gemini":
            try:
                from agno.models.google import Gemini

                cls.PROVIDERS["gemini"] = Gemini
                return Gemini
            except ImportError:
                return None
        return None

    def __init__(self, settings: Settings):
        self.settings = settings
        self.context_windows = ContextWindowResolver()

        # Resolve cloud credentials for inference routing
        self._cloud_token = CloudCredentials(settings.auth.credentials_file).access_token
        self._cloud_server_url = settings.api_url if self._cloud_token else None

    def _build_gemini_kwargs(self, entry: dict[str, Any], api_key: str | None) -> dict[str, Any]:
        """Gemini uses its own SDK with a slimmer kwarg surface than
        OpenAI-like providers (no ``base_url``, no ``http_client``)."""
        kwargs: dict[str, Any] = {"id": entry["model_id"]}
        if api_key:
            kwargs["api_key"] = api_key
        if "temperature" in entry:
            kwargs["temperature"] = entry["temperature"]
        if "max_tokens" in entry:
            kwargs["max_tokens"] = entry["max_tokens"]
        return kwargs

    def _build_openai_like_kwargs(
        self, entry: dict[str, Any], api_key: str | None
    ) -> dict[str, Any]:
        """OpenAI-like providers (OpenAI, xAI, Ember Cloud gateway,
        etc.) share a broad kwarg surface: base_url, api_key,
        temperature, max_tokens, timeout + http_client.

        Models with explicit credentials use them directly.
        Otherwise, authenticated users route through the Ember Cloud
        gateway. URL and API key resolve independently:
        - URL: from model entry, or Ember Cloud gateway as fallback
        - Key: from model entry, or Ember Cloud token as fallback
        """
        kwargs: dict[str, Any] = {"id": entry["model_id"]}

        if "url" in entry:
            kwargs["base_url"] = entry["url"]

        if api_key == "cloud_token":
            kwargs["api_key"] = self._cloud_token or "not-set"
        elif api_key:
            kwargs["api_key"] = api_key
        else:
            kwargs["api_key"] = "not-set"

        if "temperature" in entry:
            kwargs["temperature"] = entry["temperature"]
        if "max_tokens" in entry:
            kwargs["max_tokens"] = entry["max_tokens"]

        # Request timeout — prevents indefinite hangs when the server
        # or upstream provider stops responding. Configurable per
        # model via ``timeout`` in the registry entry; defaults to
        # 60s. The same value goes on BOTH the OpenAI-SDK ``timeout``
        # kwarg AND the underlying ``httpx.AsyncClient`` we pass in —
        # without setting it on the AsyncClient too, the SDK-level
        # timeout is shadowed by httpx's defaults and hung
        # connections can wedge forever.
        timeout_s = entry.get("timeout", 60)
        kwargs["timeout"] = timeout_s

        # Short keepalive expiry avoids stale connections that hang
        # when reused after idle periods (e.g. between user
        # messages).
        kwargs["http_client"] = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s),
            limits=httpx.Limits(
                max_connections=10,
                max_keepalive_connections=5,
                keepalive_expiry=30,
            ),
        )
        return kwargs

    def get_model(self, name: str | None = None) -> OpenAILike:
        """Get an Agno model instance by registry name.

        When no model resolves (registry empty AND no default —
        e.g. brand-new install before ``/login``, or a stale token
        that returned no entries from cloud discovery) we return a
        :class:`_NoModelConfigured` placeholder so the session can
        still construct. Real invocation raises a clear error; the
        TUI stays reachable so the user can run ``/login``.
        """
        if name is None or name == "":
            name = self._effective_default(strict=False)
        if not name:
            logger.warning(
                "No model configured — returning placeholder. "
                "Run /login or add a model to models.registry."
            )
            return _NoModelConfigured()

        entry = self._resolve_entry(name)
        if entry is None:
            raise ValueError(
                f"Unknown model: '{name}'. Add it to models.registry in your config, "
                f"or use the 'provider:model_id' format (e.g., 'openai_like:gpt-4o')."
            )

        provider_name = entry.get("provider", "openai_like")
        provider_cls = self.PROVIDERS.get(provider_name) or self._load_provider(provider_name)
        if provider_cls is None:
            raise ValueError(
                f"Unknown provider: '{provider_name}'. Available: {list(self.PROVIDERS.keys())}. "
                f"For Gemini, install: pip install google-genai"
            )

        api_key = self._resolve_api_key(entry)

        if provider_name == "gemini":
            return provider_cls(**self._build_gemini_kwargs(entry, api_key))

        # OpenAI-like providers wrapped in the logging trace model.
        model = _LoggingModel(**self._build_openai_like_kwargs(entry, api_key))
        model._vision = entry.get("vision", False)
        return model

    def get_context_window(self, name: str | None = None) -> int:
        """Get the context window size for a model.

        Bootstrap-safe: when no model is configured (registry empty,
        no default) we return the configured ``max_context_window``
        instead of raising. The session must be able to construct
        even when cloud discovery hasn't populated the registry yet
        — otherwise the user can't reach ``/login`` to fix it.
        """
        if name is None or name == "":
            name = self._effective_default(strict=False)
        if not name:
            return self.settings.models.max_context_window
        entry = self._resolve_entry(name)
        model_id = entry["model_id"] if entry else name
        return self.context_windows.resolve(model_id, entry)

    async def aget_context_window(self, name: str | None = None) -> int:
        """Get the context window size, with async API fallback."""
        if name is None or name == "":
            name = self._effective_default(strict=False)
        if not name:
            return self.settings.models.max_context_window
        entry = self._resolve_entry(name)
        model_id = entry["model_id"] if entry else name
        return await self.context_windows.aresolve(model_id, entry)

    def register_provider(self, name: str, cls: type) -> None:
        """Register a custom provider class."""
        self.PROVIDERS[name] = cls

    def _effective_default(self, *, strict: bool = True) -> str:
        """Return the active default model name.

        Resolution order:

        1. ``settings.models.default`` if explicitly set (user override,
           ``/model`` switch, or cloud-discovery auto-assign).
        2. First key in ``settings.models.registry`` — works as soon as
           cloud discovery has merged at least one entry.
        3. ``strict=True`` (default, for ``get_model`` — calls that
           actually need a working model): raise with an actionable
           "run /login" message.
        4. ``strict=False`` (bootstrap calls — context-window lookups
           during ``Session.__init__``): return ``""`` so the session
           can still construct. Raising here would brick the binary
           before the user can reach ``/login`` to fix the underlying
           problem (no cloud token, org-membership 403, network down,
           etc.).
        """
        explicit = self.settings.models.default
        if explicit:
            return explicit
        if self.settings.models.registry:
            return next(iter(self.settings.models.registry))
        if not strict:
            return ""
        raise ValueError(
            "No model configured. Run `/login` to discover hosted "
            "models from Ember Cloud, or add an entry to "
            "`models.registry` in ~/.ember/config.yaml."
        )

    def _resolve_entry(self, name: str) -> dict[str, Any] | None:
        """Resolve a model name to a registry entry."""
        if name in self.settings.models.registry:
            return self.settings.models.registry[name]
        if ":" in name:
            provider, model_id = name.split(":", 1)
            return {"provider": provider, "model_id": model_id}
        return None

    @staticmethod
    def _resolve_api_key(entry: dict[str, Any]) -> str | None:
        """Resolve API key: direct value, env var, command, or stored credentials."""
        key = resolve_api_key(entry)
        if key:
            return key

        # Fall back to stored login credentials for Ember-hosted models
        if "ignite-ember.sh" in entry.get("url", ""):
            return CloudCredentials().access_token

        return None

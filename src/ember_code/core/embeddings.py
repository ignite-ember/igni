"""In-process embedder backed by ``sentence-transformers``.

Loads ``sentence-transformers/all-MiniLM-L6-v2`` once per process and
exposes both async helpers (for our own code paths) and a chromadb-
compatible :class:`EmbeddingFunction` adapter (for collections that
auto-embed on add/query). 384-dim cosine-friendly outputs.

Why a custom wrapper instead of chromadb's built-in
``SentenceTransformerEmbeddingFunction``: HuggingFace's first-time
download chatter goes to stdout, which corrupts the BE process's
``READY`` protocol on the parent pipe. We suppress stdout around the
load step, then keep the model object as a per-model-name singleton
owned by :class:`Embedder`.

Design note: the model + its cache lookup + its lazy load are grouped
onto :class:`Embedder` (an OOP-audit fix collapsing the previous
module-level ``_lock`` / ``_model`` mutable state and its satellite
free functions). The module still re-exports thin convenience wrappers
(``embed``, ``embed_batch``, ``embed_sync``, ``get_model``,
``reset_for_tests``, ``_hf_cache_dir``, ``_is_model_cached``) so
existing callers and tests continue to work unchanged — each is a
one-liner delegating to :meth:`Embedder.shared`.

:class:`EmbeddingFunctionConfig` (Pydantic) is kept inline rather than
split into a ``schemas.py`` — the file is small and this is the only
schema it owns; splitting would be premature.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

# Module-level import with attribute-lookup at call sites — documented
# Rule 2 pattern used elsewhere in the codebase to keep test patches
# on the source module (``sentence_transformers.SentenceTransformer``)
# effective. A ``from … import SentenceTransformer`` would bind the
# class at import time, breaking the ``patch("sentence_transformers.
# SentenceTransformer")`` contract.
import sentence_transformers as _sentence_transformers
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIMENSIONS = 384


class EmbeddingFunctionConfig(BaseModel):
    """Serialized config for :class:`EmbeddingFunction`.

    Kept as a plain single-field model so :meth:`model_dump` produces
    exactly ``{"model_name": "..."}`` — this shape is the on-disk
    ChromaDB collection-config JSON contract, so a rename or alias here
    would silently break every existing collection.
    """

    # Silence Pydantic v2's ``model_`` protected-namespace warning
    # without renaming the field (which would break the on-disk JSON
    # contract described above).
    model_config = ConfigDict(protected_namespaces=())

    model_name: str = DEFAULT_MODEL


class Embedder:
    """Owns a single lazily-loaded ``SentenceTransformer`` and its lock.

    Replaces the previous module-level ``_lock``/``_model`` singleton
    plus its free-function satellites (``_hf_cache_dir``,
    ``_is_model_cached``, ``_load_model``, ``get_model``,
    ``embed_sync``, ``embed_batch``, ``embed``, ``reset_for_tests``).

    The process-wide instance registry lives on the class as
    :attr:`_INSTANCES`, keyed by model name. Use :meth:`shared` to look
    up or create the per-name instance — construction is cheap (no
    model load), the load is deferred to :meth:`get_model` on first
    use.
    """

    # Class-level registry of per-model-name singletons.
    _INSTANCES: dict[str, Embedder] = {}
    _REGISTRY_LOCK = threading.Lock()

    def __init__(self, model_name: str = DEFAULT_MODEL) -> None:
        self._model_name = model_name
        self._model: Any | None = None  # SentenceTransformer; Any avoids eager import.
        self._lock = threading.Lock()

    @property
    def model_name(self) -> str:
        return self._model_name

    @classmethod
    def shared(cls, model_name: str = DEFAULT_MODEL) -> Embedder:
        """Return the process-wide :class:`Embedder` for ``model_name``.

        Cheap: just a dict lookup / instance creation. Does NOT trigger
        a model load — that stays lazy behind :meth:`get_model`.
        """
        existing = cls._INSTANCES.get(model_name)
        if existing is not None:
            return existing
        with cls._REGISTRY_LOCK:
            existing = cls._INSTANCES.get(model_name)
            if existing is None:
                existing = cls(model_name)
                cls._INSTANCES[model_name] = existing
            return existing

    @classmethod
    def reset_all(cls) -> None:
        """Tear down every registered singleton. Intended for tests."""
        with cls._REGISTRY_LOCK:
            for embedder in cls._INSTANCES.values():
                embedder.reset()
            cls._INSTANCES.clear()

    @staticmethod
    def hub_cache_dir() -> Path:
        """Resolve the HuggingFace hub cache directory.

        Honors ``HF_HOME`` first (the modern env var), then
        ``HF_HUB_CACHE`` (legacy), then the default
        ``~/.cache/huggingface/hub``.
        """
        env_hf_home = os.environ.get("HF_HOME")
        if env_hf_home:
            return Path(env_hf_home).expanduser() / "hub"
        env_hub = os.environ.get("HF_HUB_CACHE")
        if env_hub:
            return Path(env_hub).expanduser()
        return Path.home() / ".cache" / "huggingface" / "hub"

    @staticmethod
    def is_model_cached(model_name: str = DEFAULT_MODEL) -> bool:
        """Return True if ``model_name`` already exists in the HF hub cache.

        HF's on-disk format is ``models--<org>--<model>`` and contains
        a ``snapshots/<sha>`` directory once a download has completed.
        """
        cache_dir = Embedder.hub_cache_dir()
        safe_name = "models--" + model_name.replace("/", "--")
        candidate = cache_dir / safe_name / "snapshots"
        if not candidate.is_dir():
            return False
        return any(candidate.iterdir())

    def get_model(self) -> Any:
        """Return the wrapped ``SentenceTransformer``, loading on first call.

        Cheap on subsequent calls — the model object is reused.
        """
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is None:
                logger.debug("Loading sentence-transformer model: %s", self._model_name)
                self._model = self._load()
        return self._model

    def _load(self) -> Any:
        """Construct the ``SentenceTransformer`` with stdout silenced.

        Two startup speedups carried over from the previous design:

        - **Stdout suppress**: HuggingFace's first-time download
          chatter goes to fd 1, which corrupts the BE process's READY
          handshake on the parent pipe. We redirect fd 1 to
          ``/dev/null`` while constructing the model.
        - **Offline fast-path**: when the model is already in the HF
          hub cache, we set ``HF_HUB_OFFLINE=1`` for the load so
          ``SentenceTransformer(...)`` skips the network probe that
          otherwise checks the hub for updates. Cuts warm cold-start
          from ~60s (network timeout) to ~2s on flaky connections.
        """
        offline_env = os.environ.get("HF_HUB_OFFLINE")
        use_offline = offline_env is None and Embedder.is_model_cached(self._model_name)
        if use_offline:
            os.environ["HF_HUB_OFFLINE"] = "1"
            logger.debug("Loading %s from HF cache in offline mode", self._model_name)

        devnull_fd = os.open(os.devnull, os.O_WRONLY)
        saved_stdout = os.dup(1)
        try:
            os.dup2(devnull_fd, 1)
            # Attribute-lookup on the module (not a captured symbol) so
            # ``patch("sentence_transformers.SentenceTransformer")``
            # replaces the class we resolve here.
            return _sentence_transformers.SentenceTransformer(self._model_name)
        finally:
            os.dup2(saved_stdout, 1)
            os.close(saved_stdout)
            os.close(devnull_fd)
            if use_offline:
                del os.environ["HF_HUB_OFFLINE"]

    def encode(self, texts: Iterable[str]) -> list[list[float]]:
        """Synchronous encode of one or many texts."""
        text_list = list(texts)
        if not text_list:
            return []
        model = self.get_model()
        array = model.encode(text_list, show_progress_bar=False, convert_to_numpy=True)
        return [list(map(float, row)) for row in array]

    async def encode_batch_async(self, texts: Iterable[str]) -> list[list[float]]:
        """Async wrapper that runs :meth:`encode` on a worker thread."""
        text_list = list(texts)
        if not text_list:
            return []
        return await asyncio.to_thread(self.encode, text_list)

    async def encode_one_async(self, text: str) -> list[float]:
        """Async single-text wrapper."""
        result = await self.encode_batch_async([text])
        return result[0] if result else []

    def reset(self) -> None:
        """Drop the cached model — only for tests that need a clean slate."""
        with self._lock:
            self._model = None


# --- Module-level convenience wrappers ---------------------------------------
# Thin delegates that preserve backwards compatibility with existing callers
# (``prefetch_models.py``, ``chroma_client_factory.py``,
# ``knowledge/collections.py``) and every test in ``tests/test_embeddings.py``
# that pokes ``embeddings.<name>`` directly. Each is a one-liner.


def _hf_cache_dir() -> Path:
    """Deprecated shim; call :meth:`Embedder.hub_cache_dir` directly."""
    return Embedder.hub_cache_dir()


def _is_model_cached(model_name: str = DEFAULT_MODEL) -> bool:
    """Deprecated shim; call :meth:`Embedder.is_model_cached` directly."""
    return Embedder.is_model_cached(model_name)


def get_model() -> Any:
    """Return the process-wide ``SentenceTransformer`` for the default model."""
    return Embedder.shared(DEFAULT_MODEL).get_model()


def embed_sync(texts: Iterable[str]) -> list[list[float]]:
    """Synchronous embed of one or many texts using the default model."""
    return Embedder.shared(DEFAULT_MODEL).encode(texts)


async def embed_batch(texts: Iterable[str]) -> list[list[float]]:
    """Async wrapper that runs the encode on a worker thread."""
    return await Embedder.shared(DEFAULT_MODEL).encode_batch_async(texts)


async def embed(text: str) -> list[float]:
    """Async single-text wrapper."""
    return await Embedder.shared(DEFAULT_MODEL).encode_one_async(text)


def reset_for_tests() -> None:
    """Drop the cached model for the default embedder — for tests only."""
    Embedder.shared(DEFAULT_MODEL).reset()


class EmbeddingFunction:
    """ChromaDB-compatible embedding function backed by a shared :class:`Embedder`.

    Implements just enough of chromadb's
    ``EmbeddingFunction[Documents]`` protocol to be plugged into a
    collection: ``__call__``, ``name``, ``get_config``, and
    ``build_from_config``. Everything else inherits the protocol's
    defaults.
    """

    def __init__(self, model_name: str = DEFAULT_MODEL):
        self.model_name = model_name
        # Cheap — no model load happens here; :meth:`Embedder.get_model`
        # stays lazy so ChromaDB's eager EF instantiation during
        # collection open does not trigger a HF hub round-trip.
        self._embedder = Embedder.shared(model_name)

    def __call__(self, input: list[str]) -> list[list[float]]:
        return self._embedder.encode(input)

    def embed_query(self, input: list[str]) -> list[list[float]]:
        # ChromaDB calls this on query side. The protocol docs say it
        # defaults to ``__call__`` but the runtime check requires the
        # method to be defined explicitly.
        return self._embedder.encode(input)

    def is_legacy(self) -> bool:
        # ChromaDB's collection-config logger checks this; declaring it
        # avoids the ``legacy embedding function config`` warning.
        return False

    @staticmethod
    def name() -> str:
        return "ember-sentence-transformer"

    def default_space(self) -> str:
        return "cosine"

    def supported_spaces(self) -> list[str]:
        return ["cosine", "l2", "ip"]

    def get_config(self) -> dict[str, Any]:
        return EmbeddingFunctionConfig(model_name=self.model_name).model_dump()

    @staticmethod
    def build_from_config(config: dict[str, Any]) -> EmbeddingFunction:
        parsed = EmbeddingFunctionConfig.model_validate(config)
        return EmbeddingFunction(model_name=parsed.model_name)

    def validate_config_update(
        self, old_config: dict[str, Any], new_config: dict[str, Any]
    ) -> None:
        EmbeddingFunctionConfig.model_validate(old_config)
        EmbeddingFunctionConfig.model_validate(new_config)
        return None

    @staticmethod
    def validate_config(config: dict[str, Any]) -> None:
        EmbeddingFunctionConfig.model_validate(config)
        return None

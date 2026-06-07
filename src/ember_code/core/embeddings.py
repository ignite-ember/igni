"""In-process embedder backed by ``sentence-transformers``.

Loads ``sentence-transformers/all-MiniLM-L6-v2`` once per process and
exposes both async helpers (for our own code paths) and a chromadb-
compatible :class:`EmbeddingFunction` adapter (for collections that
auto-embed on add/query). 384-dim cosine-friendly outputs.

Why a custom wrapper instead of chromadb's built-in
``SentenceTransformerEmbeddingFunction``: HuggingFace's first-time
download chatter goes to stdout, which corrupts the BE process's
``READY`` protocol on the parent pipe. We suppress stdout around the
load step, then keep the model object as a module-level singleton.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIMENSIONS = 384

_lock = threading.Lock()
_model: Any | None = None  # SentenceTransformer; typed Any to avoid eager import.


def _hf_cache_dir() -> Path:
    """Resolve the HuggingFace hub cache directory.

    Honors ``HF_HOME`` first (the modern env var), then ``HF_HUB_CACHE``
    (legacy), then the default ``~/.cache/huggingface/hub``.
    """
    env_hf_home = os.environ.get("HF_HOME")
    if env_hf_home:
        return Path(env_hf_home).expanduser() / "hub"
    env_hub = os.environ.get("HF_HUB_CACHE")
    if env_hub:
        return Path(env_hub).expanduser()
    return Path.home() / ".cache" / "huggingface" / "hub"


def _is_model_cached(model_name: str = DEFAULT_MODEL) -> bool:
    """Return True if the model already exists in the HF hub cache.

    HF's on-disk format is ``models--<org>--<model>`` and contains a
    ``snapshots/<sha>`` directory once a download has completed.
    """
    cache_dir = _hf_cache_dir()
    safe_name = "models--" + model_name.replace("/", "--")
    candidate = cache_dir / safe_name / "snapshots"
    if not candidate.is_dir():
        return False
    return any(candidate.iterdir())


def _load_model() -> Any:
    """Construct the ``SentenceTransformer`` with stdout silenced.

    Two startup speedups carried over from the previous design:

    - **Stdout suppress**: HuggingFace's first-time download chatter
      goes to fd 1, which corrupts the BE process's READY handshake on
      the parent pipe. We redirect fd 1 to ``/dev/null`` while
      constructing the model.
    - **Offline fast-path**: when the model is already in the HF hub
      cache, we set ``HF_HUB_OFFLINE=1`` for the load so
      ``SentenceTransformer(...)`` skips the network probe that
      otherwise checks the hub for updates. Cuts warm cold-start from
      ~60s (network timeout) to ~2s on flaky connections.
    """
    from sentence_transformers import SentenceTransformer

    offline_env = os.environ.get("HF_HUB_OFFLINE")
    use_offline = offline_env is None and _is_model_cached()
    if use_offline:
        os.environ["HF_HUB_OFFLINE"] = "1"
        logger.debug("Loading %s from HF cache in offline mode", DEFAULT_MODEL)

    devnull_fd = os.open(os.devnull, os.O_WRONLY)
    saved_stdout = os.dup(1)
    try:
        os.dup2(devnull_fd, 1)
        return SentenceTransformer(DEFAULT_MODEL)
    finally:
        os.dup2(saved_stdout, 1)
        os.close(saved_stdout)
        os.close(devnull_fd)
        if use_offline:
            del os.environ["HF_HUB_OFFLINE"]


def get_model() -> Any:
    """Return the process-wide ``SentenceTransformer``, loading on first call.

    Cheap on subsequent calls — the model object is reused.
    """
    global _model
    if _model is not None:
        return _model
    with _lock:
        if _model is None:
            logger.debug("Loading sentence-transformer model: %s", DEFAULT_MODEL)
            _model = _load_model()
    return _model


def embed_sync(texts: Iterable[str]) -> list[list[float]]:
    """Synchronous embed of one or many texts.

    Used by the chromadb :class:`EmbeddingFunction` adapter and by any
    sync caller that already runs in a worker thread.
    """
    text_list = list(texts)
    if not text_list:
        return []
    model = get_model()
    array = model.encode(text_list, show_progress_bar=False, convert_to_numpy=True)
    return [list(map(float, row)) for row in array]


async def embed_batch(texts: Iterable[str]) -> list[list[float]]:
    """Async wrapper that runs the encode on a worker thread."""
    text_list = list(texts)
    if not text_list:
        return []
    return await asyncio.to_thread(embed_sync, text_list)


async def embed(text: str) -> list[float]:
    """Async single-text wrapper."""
    result = await embed_batch([text])
    return result[0] if result else []


def reset_for_tests() -> None:
    """Drop the cached model — only for tests that need a clean slate."""
    global _model
    with _lock:
        _model = None


class EmbeddingFunction:
    """ChromaDB-compatible embedding function backed by the shared singleton.

    Implements just enough of chromadb's
    ``EmbeddingFunction[Documents]`` protocol to be plugged into a
    collection: ``__call__``, ``name``, ``get_config``, and
    ``build_from_config``. Everything else inherits the protocol's
    defaults.
    """

    def __init__(self, model_name: str = DEFAULT_MODEL):
        self.model_name = model_name

    def __call__(self, input: list[str]) -> list[list[float]]:
        return embed_sync(input)

    def embed_query(self, input: list[str]) -> list[list[float]]:
        # ChromaDB calls this on query side. The protocol docs say it
        # defaults to ``__call__`` but the runtime check requires the
        # method to be defined explicitly.
        return embed_sync(input)

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
        return {"model_name": self.model_name}

    @staticmethod
    def build_from_config(config: dict[str, Any]) -> EmbeddingFunction:
        return EmbeddingFunction(model_name=config.get("model_name", DEFAULT_MODEL))

    def validate_config_update(
        self, old_config: dict[str, Any], new_config: dict[str, Any]
    ) -> None:
        return None

    @staticmethod
    def validate_config(config: dict[str, Any]) -> None:
        return None

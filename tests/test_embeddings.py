"""Tests for the in-process sentence-transformer embedder."""

from __future__ import annotations

import math
import os
from unittest.mock import patch

import pytest

from ember_code.core import embeddings


@pytest.fixture
def reset_model_cache():
    """Drop the singleton between tests that care about load semantics."""
    embeddings.reset_for_tests()
    yield
    embeddings.reset_for_tests()


class TestSync:
    def test_empty_input_returns_empty(self):
        assert embeddings.embed_sync([]) == []

    def test_returns_correct_dimensions(self):
        out = embeddings.embed_sync(["hello world"])
        assert len(out) == 1
        assert len(out[0]) == embeddings.EMBEDDING_DIMENSIONS
        assert all(isinstance(x, float) for x in out[0])

    def test_batch_returns_one_row_per_input(self):
        out = embeddings.embed_sync(["alpha", "beta", "gamma"])
        assert len(out) == 3
        assert all(len(row) == embeddings.EMBEDDING_DIMENSIONS for row in out)

    def test_singleton_reuses_model(self, reset_model_cache):
        # First call loads.
        first_model = embeddings.get_model()
        # Second call must return the same instance.
        assert embeddings.get_model() is first_model

    def test_similar_texts_have_similar_embeddings(self):
        a, b, c = embeddings.embed_sync(
            [
                "Authentication validates user credentials.",
                "User authentication checks login details.",
                "The pasta sauce simmered for hours on the stove.",
            ]
        )
        sim_ab = _cosine(a, b)
        sim_ac = _cosine(a, c)
        # Sentences (a, b) about auth should be more similar than (a, c).
        assert sim_ab > sim_ac


class TestAsync:
    @pytest.mark.asyncio
    async def test_embed_single(self):
        vec = await embeddings.embed("hello")
        assert len(vec) == embeddings.EMBEDDING_DIMENSIONS

    @pytest.mark.asyncio
    async def test_embed_batch(self):
        out = await embeddings.embed_batch(["one", "two"])
        assert len(out) == 2
        assert all(len(row) == embeddings.EMBEDDING_DIMENSIONS for row in out)

    @pytest.mark.asyncio
    async def test_empty_async(self):
        assert await embeddings.embed_batch([]) == []


class TestCacheFastPath:
    """The HF cache short-circuit cuts warm cold-start ~60s → ~2s."""

    def test_cache_dir_honors_hf_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HF_HOME", str(tmp_path))
        assert embeddings._hf_cache_dir() == tmp_path / "hub"

    def test_cache_dir_honors_hf_hub_cache(self, tmp_path, monkeypatch):
        monkeypatch.delenv("HF_HOME", raising=False)
        monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "custom"))
        assert embeddings._hf_cache_dir() == tmp_path / "custom"

    def test_is_model_cached_false_for_empty_cache(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HF_HOME", str(tmp_path))
        monkeypatch.delenv("HF_HUB_CACHE", raising=False)
        assert embeddings._is_model_cached() is False

    def test_is_model_cached_true_when_snapshot_present(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HF_HOME", str(tmp_path))
        monkeypatch.delenv("HF_HUB_CACHE", raising=False)
        # HF on-disk format: <hub>/models--<org>--<model>/snapshots/<sha>
        snapshots = (
            tmp_path
            / "hub"
            / "models--sentence-transformers--all-MiniLM-L6-v2"
            / "snapshots"
            / "deadbeef"
        )
        snapshots.mkdir(parents=True)
        assert embeddings._is_model_cached() is True

    def test_load_sets_hf_hub_offline_when_cached(self, reset_model_cache, tmp_path, monkeypatch):
        # Simulate a populated cache and verify the load enters offline mode.
        monkeypatch.setenv("HF_HOME", str(tmp_path))
        monkeypatch.delenv("HF_HUB_CACHE", raising=False)
        monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
        snapshots = (
            tmp_path
            / "hub"
            / "models--sentence-transformers--all-MiniLM-L6-v2"
            / "snapshots"
            / "deadbeef"
        )
        snapshots.mkdir(parents=True)

        captured: dict[str, str | None] = {}

        class _FakeModel:
            def __init__(self, _name):
                captured["offline"] = os.environ.get("HF_HUB_OFFLINE")

            def encode(self, *_args, **_kwargs):
                import numpy as np

                return np.zeros((1, embeddings.EMBEDDING_DIMENSIONS), dtype="float32")

        with patch("sentence_transformers.SentenceTransformer", _FakeModel):
            embeddings.get_model()

        assert captured["offline"] == "1"
        # Outside the load, the env var is restored (deleted).
        assert "HF_HUB_OFFLINE" not in os.environ

    def test_load_skips_offline_when_no_cache(self, reset_model_cache, tmp_path, monkeypatch):
        monkeypatch.setenv("HF_HOME", str(tmp_path))
        monkeypatch.delenv("HF_HUB_CACHE", raising=False)
        monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)

        captured: dict[str, str | None] = {}

        class _FakeModel:
            def __init__(self, _name):
                captured["offline"] = os.environ.get("HF_HUB_OFFLINE")

            def encode(self, *_args, **_kwargs):
                import numpy as np

                return np.zeros((1, embeddings.EMBEDDING_DIMENSIONS), dtype="float32")

        with patch("sentence_transformers.SentenceTransformer", _FakeModel):
            embeddings.get_model()

        # Cache empty → don't force offline; let HF do its normal check.
        assert captured["offline"] is None


class TestChromaAdapter:
    def test_call_matches_sync(self):
        ef = embeddings.EmbeddingFunction()
        out = ef(["hello world"])
        assert len(out) == 1
        assert len(out[0]) == embeddings.EMBEDDING_DIMENSIONS

    def test_name_and_config_round_trip(self):
        ef = embeddings.EmbeddingFunction()
        cfg = ef.get_config()
        rebuilt = embeddings.EmbeddingFunction.build_from_config(cfg)
        assert rebuilt.model_name == ef.model_name
        assert rebuilt.name() == ef.name()

    def test_default_space_is_cosine(self):
        assert embeddings.EmbeddingFunction().default_space() == "cosine"


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0

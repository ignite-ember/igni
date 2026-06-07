"""Shared test fixtures."""

from pathlib import Path

import pytest
from dotenv import load_dotenv

from ember_code.core.config.settings import load_settings

# Load environment variables from .env at the repo root before any test
# runs. This lets developers keep credentials for live tests
# (EMBER_TEST_LLM_API_KEY, etc.) in .env instead of exporting them per
# shell. ``override=False`` so an explicit env var still wins over .env.
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
if _ENV_FILE.is_file():
    load_dotenv(_ENV_FILE, override=False)


@pytest.fixture
def tmp_dir(tmp_path):
    """Provide a temporary directory as Path."""
    return tmp_path


@pytest.fixture
def settings():
    """Settings instance with a test-safe model registry.

    The package no longer ships a hardcoded model (hosted models come
    from cloud discovery on session start). The fixture builds a
    minimal openai_like entry so tests can resolve a model without
    optional provider packages and without needing a real cloud
    connection.
    """
    s = load_settings()
    s.models.registry["MiniMax-M2.7"] = {
        "provider": "openai_like",
        "model_id": "MiniMaxAI/MiniMax-M2.7",
        "url": "https://api.ignite-ember.sh/v1",
        "api_key": "cloud_token",
        "context_window": 204_800,
        "vision": False,
    }
    s.models.default = "MiniMax-M2.7"
    return s


@pytest.fixture
def project_dir(tmp_path):
    """Temporary project directory with .ember/ structure."""
    ember_dir = tmp_path / ".ember"
    ember_dir.mkdir()
    (ember_dir / "agents").mkdir()
    (ember_dir / "skills").mkdir()
    return tmp_path


@pytest.fixture
def sample_agent_md(tmp_path):
    """Create a sample agent .md file and return its path."""
    md = tmp_path / "test-agent.md"
    md.write_text(
        "---\n"
        "name: test-agent\n"
        "description: A test agent\n"
        "tools: Read, Grep\n"
        "model: MiniMax-M2.7\n"
        "tags: test, example\n"
        "reasoning: true\n"
        "reasoning_min_steps: 2\n"
        "reasoning_max_steps: 8\n"
        "---\n"
        "You are a test agent. Do test things.\n"
    )
    return md


@pytest.fixture
def sample_skill_md(tmp_path):
    """Create a sample SKILL.md file and return its path."""
    skill_dir = tmp_path / "test-skill"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        "---\n"
        "name: test-skill\n"
        "description: A test skill\n"
        "argument-hint: <arg>\n"
        "---\n"
        "Do something with $ARGUMENTS\n"
    )
    return skill_file

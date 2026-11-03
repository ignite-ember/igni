"""``igni -m "/<skill> args"`` runs the skill, as the interactive prompt does.

A skill, a markdown command and ``/loop <prompt>`` answer with the prompt
to run. ``igni -m`` printed that prompt as information and exited 0, so a
script asking igni to run a skill got the skill's text back and no work;
its code called the skipped turn intentional. The interactive prompt has
run it as a turn since 6fee80b5, and ``-m`` now does too.

This is the real ``igni`` process against an OpenAI-compatible model on
127.0.0.1: the skill's expanded prompt is what the model receives, the
model's reply is what igni prints, and the exit code is the turn's — not
zero when the model fails (df8518e2).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from ember_code.core.paths import CONFIG_DIR
from tests.test_igni_reaches_nothing_outside_the_network import StubModel

REPLY = "Deployed to staging."


def install(home: Path, model_url: str) -> None:
    config = home / CONFIG_DIR / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        "update_check_ttl: 0\n"
        "plugins:\n"
        "  default_marketplace: false\n"
        "models:\n"
        "  default: on-prem\n"
        "  registry:\n"
        "    on-prem:\n"
        "      provider: openai_like\n"
        "      model_id: on-prem/model\n"
        f"      url: {model_url}\n"
        "      api_key: stub-key\n"
    )
    skill = home / CONFIG_DIR / "skills" / "deploy-now"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: deploy-now\ndescription: deploy somewhere\n---\n"
        "Deploy the service to $ARGUMENTS and say where.\n",
        encoding="utf-8",
    )
    commands = home / CONFIG_DIR / "commands"
    commands.mkdir(parents=True)
    (commands / "ship-it.md").write_text(
        "---\ndescription: ship somewhere\n---\nShip the release to $ARGUMENTS and say where.\n",
        encoding="utf-8",
    )


def igni(home: Path, project: Path, message: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "HOME": str(home), "NO_COLOR": "1"}
    env.pop("IGNI_AIR_GAPPED", None)
    return subprocess.run(
        [sys.executable, "-m", "ember_code", "-m", message],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def user_text(body: dict[str, Any]) -> str:
    parts = []
    for message in body.get("messages", []):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
        parts.append(str(content))
    return "\n".join(parts)


@pytest.mark.parametrize(
    ("command", "prompt"),
    [
        ("/deploy-now staging", "Deploy the service to staging and say where."),
        ("/ship-it staging", "Ship the release to staging and say where."),
    ],
    ids=["skill", "markdown-command"],
)
def test_the_prompt_goes_to_the_model_and_its_reply_is_printed(tmp_path, command, prompt):
    home, project = tmp_path / "home", tmp_path / "project"
    project.mkdir()
    with StubModel(REPLY) as stub:
        install(home, stub.url)
        run = igni(home, project, command)

    assert run.returncode == 0, run.stderr
    sent = [user_text(b) for b in stub.bodies]
    assert any(prompt in s for s in sent), sent
    assert REPLY in run.stdout
    # The prompt is the turn, not something printed instead of one.
    assert prompt not in run.stdout


def test_a_skill_whose_turn_fails_exits_non_zero(tmp_path):
    home, project = tmp_path / "home", tmp_path / "project"
    project.mkdir()
    with StubModel(REPLY, status=500) as stub:
        install(home, stub.url)
        run = igni(home, project, "/deploy-now staging")

    assert stub.bodies, "the skill's turn never reached the model"
    assert run.returncode != 0
    assert REPLY not in run.stdout

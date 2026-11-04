"""A plugin reload must not undo the user's runtime choices.

``reload_plugins`` re-runs ``_init_plugins_output_styles_hooks``, which
(a) reset ``_active_output_style`` to the config default and (b) built a
brand-new ``ToolEventHookFactory`` — and the factory is where the live
``PermissionEvaluator`` is cached. A replacement with an empty cache
rebuilt the evaluator from ``settings.permissions.mode``, so a user who
had put the session into plan mode — i.e. told the product not to touch
their tree — was silently returned to ``default`` by installing a
plugin. The FE's mode badge reads the same evaluator, so it flipped too,
with no message.

Both are user decisions made after boot; re-reading the config is only
right for the values the user has not overridden.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from ember_code.core.config.permission_eval import PermissionMode
from ember_code.core.config.settings import Settings, load_settings
from ember_code.core.paths import CONFIG_DIR

#: Enough of a model registry for a Session to construct.
_STUB_MODEL: dict[str, Any] = {
    "default": "stub",
    "registry": {
        "stub": {
            "provider": "openai_like",
            "model_id": "stub-1",
            "url": "http://localhost:9/v1",
            "api_key": "x",
            "context_window": 8192,
        }
    },
}


def _repo(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "f.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"],
        cwd=tmp_path,
        check=True,
    )
    return tmp_path


def _settings(project: Path, *, home: Path) -> Settings:
    config = project / CONFIG_DIR
    config.mkdir(parents=True, exist_ok=True)
    (config / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "code_index": {"enabled": False},
                "knowledge": {"enabled": False},
                "models": _STUB_MODEL,
                "storage": {"data_dir": str(home)},
            }
        )
    )
    return load_settings(project_dir=project)


@pytest.fixture
def session(tmp_path: Path):
    from ember_code.core.session.core import Session

    project = _repo(tmp_path / "proj")
    return Session(settings=_settings(project, home=tmp_path / "home"), project_dir=project)


def test_permission_mode_survives_a_plugin_reload(session) -> None:
    session.set_permission_mode(PermissionMode.PLAN.value)
    assert session.permission_mode_value == "plan"

    session.reload_plugins()

    # Read through the same accessor the FE's mode badge is built from.
    assert session.permission_mode_value == "plan", (
        "a plugin install put the session back into default mode — the user "
        "had told it not to touch their tree"
    )
    # And the evaluator every freshly-built ToolEventHook is handed —
    # i.e. the one the next tool call is judged by — agrees.
    assert session._tool_hook_factory.permission_evaluator.mode is PermissionMode.PLAN


def _write_styles(tmp_path: Path) -> Path:
    """Two styles, so the boot default and the user's pick differ."""
    styles = tmp_path / "proj" / CONFIG_DIR / "output-styles"
    styles.mkdir(parents=True, exist_ok=True)
    (styles / "default.md").write_text("---\nname: default\n---\n\nSay it plainly.\n")
    (styles / "terse.md").write_text("---\nname: terse\n---\n\nBe brief.\n")
    return styles


def test_output_style_survives_a_plugin_reload(session, tmp_path: Path) -> None:
    _write_styles(tmp_path)
    # Re-discover so the freshly written styles are known to the session.
    session._init_plugins_output_styles_hooks(session.settings)
    assert session.active_output_style == "default"

    assert "Error" not in session.set_output_style("terse")
    assert session.active_output_style == "terse"

    session.reload_plugins()

    assert session.active_output_style == "terse"
    # The style's body is re-applied from the name at build time, so
    # the rebuilt team must still carry it.
    assert any(isinstance(i, str) and "Be brief." in i for i in session.main_team.instructions), (
        "the style name survived but its body did not reach the rebuilt agent"
    )


def test_a_removed_style_falls_back_instead_of_sticking(session, tmp_path: Path) -> None:
    # The control on the fix: preserving the user's choice must not
    # pin a style that no longer exists (an uninstalled plugin's).
    styles = _write_styles(tmp_path)
    session._init_plugins_output_styles_hooks(session.settings)
    session.set_output_style("terse")

    (styles / "terse.md").unlink()
    session.reload_plugins()

    assert session.active_output_style == "default"

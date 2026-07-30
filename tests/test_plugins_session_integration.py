"""Integration tests: plugins on disk → activated inside ``Session``.

The Day-1 / Day-2 apply tests exercise ``PluginLoader.apply_to_*``
directly. These tests go one level higher: drop a real plugin
directory under one of the discovery roots, instantiate a
``Session``, and assert the plugin's contents land in the right
pools / collections without any explicit apply call from the test.

Catches regressions where a future refactor of ``Session.__init__``
silently bypasses the apply step (e.g. moves it before the pools
are constructed, or wraps it in a feature flag that defaults off).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from ember_code.core.config.settings import Settings
from ember_code.core.session.core import Session

# ── Helpers ─────────────────────────────────────────────────────────


def _write_plugin(
    root: Path,
    name: str,
    *,
    with_skill: bool = False,
    with_agent: bool = False,
) -> Path:
    plugin_dir = root / name
    (plugin_dir / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "version": "1.0.0"}),
        encoding="utf-8",
    )
    if with_skill:
        skill_dir = plugin_dir / "skills" / "demo"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: demo\ndescription: x\ncategory: development\n---\nBody.\n",
        )
    if with_agent:
        (plugin_dir / "agents").mkdir(parents=True)
        (plugin_dir / "agents" / "demo.md").write_text(
            "---\nname: demo\ndescription: x\ntools: [Read]\n---\nBody.\n",
        )
    return plugin_dir


def _session_under_test(tmp_path: Path):
    """Spin up a real Session against tmp_path's home/.ember roots,
    with all the heavy deps mocked **except SkillPool and AgentPool**
    (which we need to be real so plugin contents actually land).

    Builds patches manually rather than reusing ``_session_patches``
    so the two pools we care about stay un-mocked.
    """
    from tests.test_session import _session_patches

    # Build the standard patch set but drop SkillPool + AgentPool —
    # we want the real classes for the integration check.
    patches = [p for p in _session_patches() if p.attribute not in ("SkillPool", "AgentPool")]
    mocks = {p.attribute: p.start() for p in patches}
    try:
        mocks["ModelRegistry"].return_value.get_context_window.return_value = 128_000
        cc = mocks["CloudCredentials"].return_value
        cc.is_authenticated = False
        cc.access_token = None
        cc.org_id = None
        cc.org_name = None
        cc.email = None

        settings = Settings()
        settings.storage.data_dir = str(tmp_path / "ember")

        with patch.object(Path, "home", return_value=tmp_path / "home"):
            project = tmp_path / "proj"
            project.mkdir(exist_ok=True)
            session = Session(settings, project_dir=project)
        return session, patches
    except Exception:
        for p in patches:
            p.stop()
        raise


def _stop_patches(patches) -> None:
    for p in patches:
        p.stop()


# ── Plugin discovery happens at Session.__init__ ───────────────────


def test_session_discovers_plugins_on_init(tmp_path: Path) -> None:
    """A plugin under ``~/.ember/plugins/`` is discovered by the
    ``PluginLoader`` instance owned by the Session. The list is
    populated synchronously during ``__init__`` — no lazy fetch."""
    user_ember = tmp_path / "home" / ".ember" / "plugins"
    _write_plugin(user_ember, "alpha")

    session, patches = _session_under_test(tmp_path)
    try:
        names = [p.name for p in session.plugin_loader.list_plugins()]
        assert "alpha" in names
    finally:
        _stop_patches(patches)


def test_session_loads_plugin_state(tmp_path: Path) -> None:
    """The Session reads ``~/.ember/plugins.json`` at start so
    ``_disabled_plugins`` honors prior user toggles immediately —
    no race window where a disabled plugin's contents would briefly
    activate."""
    from ember_code.core.plugins.state import PluginsState, save_state

    user_ember = tmp_path / "home" / ".ember" / "plugins"
    _write_plugin(user_ember, "alpha")
    save_state(
        PluginsState(disabled=["alpha"]),
        data_dir=tmp_path / "ember",
    )

    session, patches = _session_under_test(tmp_path)
    try:
        assert session.plugin_state.disabled == ["alpha"]
        assert "alpha" in session._disabled_plugins
    finally:
        _stop_patches(patches)


def test_session_plugin_skills_land_in_skill_pool(tmp_path: Path) -> None:
    """End-to-end: plugin with ``skills/demo/SKILL.md`` shows up in
    ``session.skill_pool`` with the ``<plugin>:`` prefix. No manual
    apply call from the test."""
    user_ember = tmp_path / "home" / ".ember" / "plugins"
    _write_plugin(user_ember, "alpha", with_skill=True)

    session, patches = _session_under_test(tmp_path)
    try:
        names = {s.name for s in session.skill_pool.list_skills()}
        assert "alpha:demo" in names
    finally:
        _stop_patches(patches)


def test_session_plugin_agents_land_in_agent_pool(tmp_path: Path) -> None:
    user_ember = tmp_path / "home" / ".ember" / "plugins"
    _write_plugin(user_ember, "alpha", with_agent=True)

    session, patches = _session_under_test(tmp_path)
    try:
        names = {d.name for d in session.pool.list_agents()}
        assert "alpha:demo" in names
    finally:
        _stop_patches(patches)


def test_session_disabled_plugin_contents_are_skipped(tmp_path: Path) -> None:
    """A plugin marked disabled in ``plugins.json`` is discovered
    (visible to the panel) but its skills/agents are NOT loaded into
    the pools. This is the contract that powers ``/plugins disable
    <name>`` — the next session start sees a clean pool."""
    from ember_code.core.plugins.state import PluginsState, save_state

    user_ember = tmp_path / "home" / ".ember" / "plugins"
    _write_plugin(user_ember, "alpha", with_skill=True, with_agent=True)
    save_state(
        PluginsState(disabled=["alpha"]),
        data_dir=tmp_path / "ember",
    )

    session, patches = _session_under_test(tmp_path)
    try:
        # Plugin is still discoverable for the panel.
        assert session.plugin_loader.get("alpha") is not None
        # But its contents are NOT in the pools.
        skill_names = {s.name for s in session.skill_pool.list_skills()}
        agent_names = {d.name for d in session.pool.list_agents()}
        assert "alpha:demo" not in skill_names
        assert "alpha:demo" not in agent_names
    finally:
        _stop_patches(patches)


def test_session_no_plugins_works_cleanly(tmp_path: Path) -> None:
    """Sessions with zero plugins must still start (and the loader's
    list must be empty). This is the default state of a fresh
    install — nothing to special-case."""
    session, patches = _session_under_test(tmp_path)
    try:
        assert session.plugin_loader.list_plugins() == []
        assert session.plugin_state.disabled == []
    finally:
        _stop_patches(patches)

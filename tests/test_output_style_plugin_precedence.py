"""Plugin-shipped output styles must not outrank the project's (SWP-56).

The loader's docstring has always said plugin styles "override user but
not project"; the roots list appended plugin roots last, i.e. highest,
so an installed plugin silently replaced the project's style — the text
appended to every assistant turn's instructions.
"""

import pytest

from ember_code.core.output_styles.loader import discover_output_styles
from ember_code.core.paths import CONFIG_DIR


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def project(tmp_path):
    return tmp_path / "proj"


@pytest.fixture
def plugin(tmp_path):
    return tmp_path / "plugin"


class TestPluginPrecedence:
    def test_project_style_beats_plugin_style(self, project, plugin):
        _write(project / CONFIG_DIR / "output-styles" / "terse.md", "PROJECT VERSION\n")
        _write(plugin / "output-styles" / "terse.md", "PLUGIN VERSION\n")

        styles = discover_output_styles(
            project, plugin_roots=[(plugin, "my-plugin")], read_claude=False
        )

        assert styles["terse"].body == "PROJECT VERSION"

    def test_group_style_beats_plugin_style(self, project, plugin, tmp_path):
        group = tmp_path / "group-cache"
        _write(group / "terse.md", "GROUP VERSION\n")
        _write(plugin / "output-styles" / "terse.md", "PLUGIN VERSION\n")

        styles = discover_output_styles(
            project, plugin_roots=[(plugin, "my-plugin")], read_claude=False, group_dir=group
        )

        assert styles["terse"].body == "GROUP VERSION"

    def test_plugin_style_beats_user_global(self, project, plugin, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setattr("pathlib.Path.home", lambda: home)
        _write(home / CONFIG_DIR / "output-styles" / "terse.md", "USER VERSION\n")
        _write(plugin / "output-styles" / "terse.md", "PLUGIN VERSION\n")

        styles = discover_output_styles(
            project, plugin_roots=[(plugin, "my-plugin")], read_claude=False
        )

        assert styles["terse"].body == "PLUGIN VERSION"

    def test_plugin_style_still_loads_when_uncontested(self, project, plugin):
        _write(plugin / "output-styles" / "pirate.md", "Arr.\n")

        styles = discover_output_styles(
            project, plugin_roots=[(plugin, "my-plugin")], read_claude=False
        )

        assert styles["pirate"].body == "Arr."

"""Tests for output styles (row 52) — discovery, the
``/output-style`` slash command, and the runtime
``set_output_style`` hot-patch.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ember_code.backend.__main__ import _build_rpc_table
from ember_code.backend.command_handler import CommandHandler
from ember_code.backend.server import BackendServer
from ember_code.core.output_styles import OutputStyle, discover_output_styles
from ember_code.core.output_styles.loader import _parse_frontmatter
from ember_code.core.session.core import Session
from ember_code.protocol.rpc import RpcMethod


def _write(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)


# ── Frontmatter ──────────────────────────────────────────────


class TestParseFrontmatter:
    def test_no_frontmatter_returns_empty(self):
        meta, body = _parse_frontmatter("Just a body.\n")
        assert meta == {}
        assert body == "Just a body.\n"

    def test_valid_frontmatter(self):
        meta, body = _parse_frontmatter(
            "---\nname: explanatory\ndescription: Verbose mode\n---\nBody text.\n"
        )
        assert meta == {"name": "explanatory", "description": "Verbose mode"}
        assert body == "Body text.\n"

    def test_malformed_yaml_is_no_op(self):
        meta, body = _parse_frontmatter("---\n: : invalid:\n  :\n---\nBody.\n")
        assert meta == {}
        assert "Body" in body


# ── Discovery ─────────────────────────────────────────────────


class TestDiscover:
    def test_project_ember_style_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        (tmp_path / "home").mkdir()
        _write(
            tmp_path / ".ember" / "output-styles" / "tight.md",
            "---\ndescription: Tight + minimal\n---\nBe tight.\n",
        )
        out = discover_output_styles(tmp_path)
        assert "tight" in out
        assert out["tight"].description == "Tight + minimal"
        assert "Be tight." in out["tight"].body

    def test_user_global_style_found(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        _write(home / ".ember" / "output-styles" / "g.md", "Globally available.\n")
        out = discover_output_styles(tmp_path)
        assert "g" in out

    def test_project_overrides_user(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        _write(home / ".ember" / "output-styles" / "x.md", "USER VERSION\n")
        _write(tmp_path / ".ember" / "output-styles" / "x.md", "PROJECT VERSION\n")
        out = discover_output_styles(tmp_path)
        assert "PROJECT VERSION" in out["x"].body
        assert "USER VERSION" not in out["x"].body

    def test_read_claude_false_skips_claude_dirs(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        _write(home / ".claude" / "output-styles" / "claudeonly.md", "Should not load")
        _write(home / ".ember" / "output-styles" / "emberonly.md", "Loads")
        out = discover_output_styles(tmp_path, read_claude=False)
        assert "claudeonly" not in out
        assert "emberonly" in out

    def test_name_defaults_to_filename_stem(self, tmp_path, monkeypatch):
        """When the frontmatter doesn't carry ``name:``, the
        filename stem becomes the identifier."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        _write(tmp_path / ".ember" / "output-styles" / "concise.md", "no frontmatter\n")
        out = discover_output_styles(tmp_path)
        assert "concise" in out

    def test_explicit_name_overrides_stem(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        _write(
            tmp_path / ".ember" / "output-styles" / "filename.md",
            "---\nname: real-name\n---\nbody\n",
        )
        out = discover_output_styles(tmp_path)
        assert "real-name" in out
        assert "filename" not in out


# ── set_output_style (runtime hot-patch) ─────────────────────


class TestSetOutputStyle:
    def _session(self, styles=None):
        session = Session.__new__(Session)
        session.output_styles = styles or {
            "default": OutputStyle(
                name="default",
                path=Path("/dev/null"),
                description="Default mode",
                body="Be terse.",
            ),
            "explanatory": OutputStyle(
                name="explanatory",
                path=Path("/dev/null"),
                description="Explain everything",
                body="Explain things.",
            ),
        }
        session._active_output_style = "default"
        from ember_code.core.session.broadcast import BroadcastBus

        session.broadcast_bus = BroadcastBus()
        session.main_team = None  # patch path tolerated
        return session

    def test_switch_to_known_style(self):
        session = self._session()
        msg = session.set_output_style("explanatory")
        assert "default" in msg and "explanatory" in msg
        assert session._active_output_style == "explanatory"

    def test_switch_to_unknown_returns_available_list(self):
        session = self._session()
        msg = session.set_output_style("nonexistent")
        assert "Error" in msg
        assert "default" in msg  # listed as available
        assert "explanatory" in msg
        # State unchanged.
        assert session._active_output_style == "default"

    def test_no_op_when_already_active(self):
        session = self._session()
        msg = session.set_output_style("default")
        assert "already" in msg.lower()

    def test_hot_patches_team_instructions(self):
        """When the main team exists, switching styles strips
        the old ``# Output style: ...`` block and appends the
        new one — so the next ``arun`` picks up the new tone
        without rebuilding the team."""
        session = self._session()
        team = MagicMock()
        team.instructions = [
            "system prompt prefix",
            "# Output style: default\n\nBe terse.",
            "trailing block",
        ]
        session.main_team = team
        session.set_output_style("explanatory")
        # Old style block stripped.
        assert not any(
            isinstance(s, str) and s.startswith("# Output style: default")
            for s in team.instructions
        )
        # New style block appended.
        assert any(
            isinstance(s, str) and s.startswith("# Output style: explanatory")
            for s in team.instructions
        )

    def test_broadcasts_output_style_changed(self):
        session = self._session()
        captured: list[tuple[str, dict]] = []
        session.register_broadcast_callback(lambda ch, p: captured.append((ch, p)))
        session.set_output_style("explanatory")
        evt = next(p for ch, p in captured if ch == "output_style_changed")
        assert evt["style"] == "explanatory"
        assert evt["previous"] == "default"


# ── /output-style slash command ──────────────────────────────


class TestOutputStyleSlashCommand:
    def _make_session(self):
        session = Session.__new__(Session)
        session.output_styles = {
            "default": OutputStyle(
                name="default",
                path=Path("/dev/null"),
                description="Default mode",
                body="Be terse.",
            ),
            "explanatory": OutputStyle(
                name="explanatory",
                path=Path("/dev/null"),
                description="Explain everything",
                body="Explain.",
            ),
        }
        session._active_output_style = "default"
        from ember_code.core.session.broadcast import BroadcastBus

        session.broadcast_bus = BroadcastBus()
        session.main_team = None
        return session

    @pytest.mark.asyncio
    async def test_list_includes_active_marker(self):
        session = self._make_session()
        handler = CommandHandler(session)
        result = await handler.handle("/output-style list")
        assert "default" in result.content
        assert "explanatory" in result.content
        assert "(active)" in result.content

    @pytest.mark.asyncio
    async def test_bare_lists(self):
        """``/output-style`` with no args defaults to ``list``."""
        session = self._make_session()
        handler = CommandHandler(session)
        result = await handler.handle("/output-style")
        assert "default" in result.content
        assert "explanatory" in result.content

    @pytest.mark.asyncio
    async def test_status_shows_active(self):
        session = self._make_session()
        handler = CommandHandler(session)
        result = await handler.handle("/output-style status")
        assert "default" in result.content.lower()

    @pytest.mark.asyncio
    async def test_set_by_bare_name(self):
        """``/output-style explanatory`` switches directly —
        ``set`` keyword is optional."""
        session = self._make_session()
        handler = CommandHandler(session)
        await handler.handle("/output-style explanatory")
        assert session._active_output_style == "explanatory"

    @pytest.mark.asyncio
    async def test_set_with_explicit_set_keyword(self):
        session = self._make_session()
        handler = CommandHandler(session)
        await handler.handle("/output-style set explanatory")
        assert session._active_output_style == "explanatory"

    @pytest.mark.asyncio
    async def test_unknown_style_returns_error(self):
        session = self._make_session()
        handler = CommandHandler(session)
        result = await handler.handle("/output-style nonexistent")
        assert "Error" in result.content
        # State unchanged.
        assert session._active_output_style == "default"

    @pytest.mark.asyncio
    async def test_empty_styles_dir_explains_how(self):
        """When no styles are configured, ``list`` returns a
        helpful hint rather than an empty block."""
        session = self._make_session()
        session.output_styles = {}
        session._active_output_style = ""
        handler = CommandHandler(session)
        result = await handler.handle("/output-style list")
        assert "No output styles" in result.content
        assert "output-styles" in result.content  # mentions the path


# ── GET_OUTPUT_STYLES RPC ────────────────────────────────────


class TestGetOutputStylesRpc:
    def test_returns_active_plus_listing(self):
        session = MagicMock()
        session.output_styles = {
            "default": OutputStyle(
                name="default",
                path=Path("/dev/null"),
                description="Default mode",
                body="x",
            ),
            "learning": OutputStyle(
                name="learning",
                path=Path("/dev/null"),
                description="Teacher mode",
                body="y",
            ),
        }
        session._active_output_style = "learning"
        session.active_output_style = "learning"
        backend = BackendServer.__new__(BackendServer)
        backend._session = session
        out = backend.get_output_styles()
        assert out.active == "learning"
        names = [s.name for s in out.styles]
        assert names == ["default", "learning"]  # sorted

    def test_returns_empty_when_no_styles(self):
        session = MagicMock(spec=["output_styles", "active_output_style"])
        session.output_styles = {}
        session.active_output_style = ""
        backend = BackendServer.__new__(BackendServer)
        backend._session = session
        out = backend.get_output_styles()
        assert out.active == ""
        assert out.styles == []

    def test_dispatch_table_routes_get_output_styles(self):
        session = MagicMock()
        session.output_styles = {
            "default": OutputStyle(
                name="default",
                path=Path("/dev/null"),
                description="d",
                body="",
            )
        }
        session._active_output_style = "default"
        session.active_output_style = "default"
        backend = BackendServer.__new__(BackendServer)
        backend._session = session

        table = _build_rpc_table(backend, transport=MagicMock(), login_state={})
        handler = table.get(RpcMethod.GET_OUTPUT_STYLES)
        assert handler is not None
        result = handler({})
        assert result.active == "default"

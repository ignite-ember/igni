"""Tests for private helpers in ``core/utils/context``:

  * ``_resolve_at_path`` — translates one ``@<token>`` to an
    absolute path under ``allowed_root``. Returns None when the
    token doesn't resolve OR points outside the root (security
    — prevents `@../../../etc/passwd`-style escapes).
  * ``_mask_code_regions`` / ``_unmask_code_regions`` — the
    pair that protects ``@<path>`` tokens inside fenced code
    blocks and inline-backtick spans from being expanded as
    imports. Round-trip identity is load-bearing.

Existing ``test_context.py`` covers the integration paths
(``load_project_context``, ``_read_with_imports``). This file
pins the individual helpers so a future refactor of the
mask-then-substitute pipeline doesn't silently break security
or @-import behaviour.
"""

from __future__ import annotations

from ember_code.core.utils.context import (
    _mask_code_regions,
    _resolve_at_path,
    _unmask_code_regions,
)

# ── _resolve_at_path ────────────────────────────────────────


class TestResolveAtPath:
    def test_relative_path_resolved_from_source_parent(self, tmp_path):
        # The most-common case: ``@subdir/file.md`` resolves
        # relative to the rules-file that contains the ``@``,
        # NOT to cwd. Mirrors how editors / CC docs resolve
        # relative imports.
        root = tmp_path
        source = root / "main.md"
        source.write_text("anchor")
        target = root / "sub" / "doc.md"
        target.parent.mkdir()
        target.write_text("imported")
        result = _resolve_at_path("sub/doc.md", source, root)
        assert result == target.resolve()

    def test_absolute_path_resolved(self, tmp_path):
        # ``@/abs/path`` is taken as absolute.
        target = tmp_path / "absolute.md"
        target.write_text("x")
        source = tmp_path / "main.md"
        source.write_text("anchor")
        result = _resolve_at_path(str(target), source, tmp_path)
        assert result == target.resolve()

    def test_tilde_expanded(self, tmp_path, monkeypatch):
        # ``@~/file`` expands to the home directory. Pinned so a
        # future refactor that drops ``expanduser()`` is a
        # deliberate change.
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        target = fake_home / "doc.md"
        target.write_text("x")
        monkeypatch.setenv("HOME", str(fake_home))
        source = tmp_path / "main.md"
        source.write_text("anchor")
        # Allow the home dir as the root.
        result = _resolve_at_path("~/doc.md", source, fake_home)
        assert result == target.resolve()

    def test_path_outside_allowed_root_returns_None(self, tmp_path):
        # **Security-critical**: ``@../../../etc/passwd``-style
        # escapes must NOT resolve outside the allowed root.
        # The Path.relative_to() check raises ValueError when
        # the candidate isn't under allowed_root.
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        outside = tmp_path / "secret.md"
        outside.write_text("don't read this")
        source = sandbox / "main.md"
        source.write_text("anchor")
        # Try to escape via ``..``.
        result = _resolve_at_path("../secret.md", source, sandbox)
        assert result is None

    def test_absolute_path_outside_root_returns_None(self, tmp_path):
        # Same security check via absolute-path attack: ``@/etc/passwd``
        # absolute path is still rejected if outside allowed_root.
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        outside = tmp_path / "secret.md"
        outside.write_text("x")
        source = sandbox / "main.md"
        source.write_text("anchor")
        result = _resolve_at_path(str(outside), source, sandbox)
        assert result is None

    def test_missing_file_returns_None(self, tmp_path):
        # File doesn't exist → None (the caller leaves the
        # literal ``@<token>`` in place).
        source = tmp_path / "main.md"
        source.write_text("anchor")
        result = _resolve_at_path("missing.md", source, tmp_path)
        assert result is None

    def test_directory_returns_None(self, tmp_path):
        # ``@<token>`` must point to a FILE, not a directory.
        # ``is_file()`` is False for directories.
        source = tmp_path / "main.md"
        source.write_text("anchor")
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        result = _resolve_at_path("subdir", source, tmp_path)
        assert result is None

    def test_oserror_swallowed_returns_None(self, tmp_path):
        # The function catches OSError + ValueError. Pin that
        # a token causing one of those returns None rather than
        # bubbling up.
        source = tmp_path / "main.md"
        source.write_text("anchor")
        # Empty token → Path("") which resolves weirdly.
        result = _resolve_at_path("", source, tmp_path)
        # Either resolves to source.parent (a directory — None
        # via is_file check) or fails — both → None.
        assert result is None


# ── _mask_code_regions / _unmask_code_regions ───────────────


class TestMaskRoundTrip:
    """Round-trip identity is the load-bearing property —
    masking then unmasking must always produce the original
    string, otherwise the @-import pipeline corrupts content."""

    def test_no_code_regions_is_noop(self):
        # Plain text → no sentinels → unmask is identity.
        original = "plain text with @path/to/foo.md mention"
        masked, originals = _mask_code_regions(original)
        assert masked == original
        assert originals == []
        assert _unmask_code_regions(masked, originals) == original

    def test_fenced_block_round_trip(self):
        # Code fence — must survive the mask → unmask cycle
        # byte-for-byte.
        original = "before\n```python\n@import this_should_stay_literal\n```\nafter"
        masked, originals = _mask_code_regions(original)
        # The fenced block should NOT appear verbatim in the
        # masked form (it's been replaced with a sentinel).
        assert "```python" not in masked
        assert "this_should_stay_literal" not in masked
        # And unmask restores byte-for-byte.
        assert _unmask_code_regions(masked, originals) == original

    def test_inline_code_round_trip(self):
        # Inline backticks — same property.
        original = "Use `@import foo` to import."
        masked, originals = _mask_code_regions(original)
        assert "@import foo" not in masked
        assert _unmask_code_regions(masked, originals) == original

    def test_both_fenced_and_inline_round_trip(self):
        # Most realistic case — both kinds in one doc.
        original = "intro `@inline.md` then\n```\n@fenced.md\n```\nand `@another` here"
        masked, originals = _mask_code_regions(original)
        # 3 stashed regions.
        assert len(originals) == 3
        # Round-trip identity.
        assert _unmask_code_regions(masked, originals) == original

    def test_sentinel_format_is_nul_delimited(self):
        # The sentinel format ``\0CODE<idx>\0`` is what the
        # ``@`` substitution pass relies on to skip masked
        # regions. Drift in the format would silently expose
        # ``@`` tokens inside code blocks to the substitution.
        original = "`code`"
        masked, originals = _mask_code_regions(original)
        assert "\0CODE0\0" in masked

    def test_originals_indexed_in_appearance_order(self):
        # The originals list is indexed in the order regions
        # were encountered (fenced first, then inline).
        original = "```\nfenced0\n```\nthen `inline1` then `inline2`"
        masked, originals = _mask_code_regions(original)
        assert len(originals) == 3
        assert "fenced0" in originals[0]
        assert "inline1" in originals[1]
        assert "inline2" in originals[2]

    def test_unmask_out_of_range_sentinel_leaves_literal(self):
        # Defensive — if the masked content is mutated and a
        # sentinel survives with an index past the originals
        # list, ``_unmask`` leaves the sentinel literal rather
        # than raising IndexError.
        masked = "before \0CODE99\0 after"
        originals: list[str] = []
        result = _unmask_code_regions(masked, originals)
        # The sentinel stays as a literal — the caller may
        # detect or strip it; we just don't crash.
        assert "\0CODE99\0" in result

    def test_unmask_with_no_sentinels_is_identity(self):
        # ``content`` has no sentinels at all → identity.
        assert _unmask_code_regions("hello world", ["unused"]) == "hello world"

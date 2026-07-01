"""Tests for the hierarchical rules discovery index."""

from __future__ import annotations

from pathlib import Path

from ember_code.core.utils.rules_index import RulesIndex


def _write(p: Path, text: str = "") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_empty_project(tmp_path: Path) -> None:
    idx = RulesIndex(tmp_path)
    assert idx.consume_path(tmp_path / "anywhere.py") == []
    assert not idx.has_pending()


def test_subdirectory_rules_found(tmp_path: Path) -> None:
    """The agent touches a file in a service subdir; the index returns
    that subdir's rules file (and only it — the project root rules are
    intentionally skipped, loaded by ``load_project_context`` instead)."""
    _write(tmp_path / "ember.md", "ROOT-LEVEL-rules")  # excluded
    _write(tmp_path / "clients" / "tauri" / "ember.md", "TAURI-rules")
    _write(tmp_path / "clients" / "tauri" / "src" / "main.ts")

    idx = RulesIndex(tmp_path)
    results = idx.consume_path(tmp_path / "clients" / "tauri" / "src" / "main.ts")

    assert len(results) == 1
    path, content = results[0]
    assert path == (tmp_path / "clients" / "tauri" / "ember.md").resolve()
    assert content == "TAURI-rules"


def test_multiple_levels_returned_shallowest_first(tmp_path: Path) -> None:
    _write(tmp_path / "clients" / "ember.md", "CLIENTS")
    _write(tmp_path / "clients" / "tauri" / "ember.md", "TAURI")
    _write(tmp_path / "clients" / "tauri" / "src" / "main.ts")

    idx = RulesIndex(tmp_path)
    results = idx.consume_path(tmp_path / "clients" / "tauri" / "src" / "main.ts")

    contents = [c for _, c in results]
    # Shallowest first — clients/ before clients/tauri/.
    assert contents == ["CLIENTS", "TAURI"]


def test_each_file_returned_at_most_once(tmp_path: Path) -> None:
    _write(tmp_path / "clients" / "tauri" / "ember.md", "TAURI")
    _write(tmp_path / "clients" / "tauri" / "src" / "main.ts")
    _write(tmp_path / "clients" / "tauri" / "src" / "other.ts")

    idx = RulesIndex(tmp_path)
    first = idx.consume_path(tmp_path / "clients" / "tauri" / "src" / "main.ts")
    second = idx.consume_path(tmp_path / "clients" / "tauri" / "src" / "other.ts")

    assert len(first) == 1
    assert second == []  # Same subtree → already shown.


def test_claude_md_picked_up_when_enabled(tmp_path: Path) -> None:
    _write(tmp_path / "service" / "CLAUDE.md", "CLAUDE-rules")
    _write(tmp_path / "service" / "f.py")

    idx = RulesIndex(tmp_path, read_claude_md=True)
    results = idx.consume_path(tmp_path / "service" / "f.py")
    assert [c for _, c in results] == ["CLAUDE-rules"]


def test_claude_md_ignored_when_disabled(tmp_path: Path) -> None:
    _write(tmp_path / "service" / "CLAUDE.md", "CLAUDE-rules")
    _write(tmp_path / "service" / "f.py")

    idx = RulesIndex(tmp_path, read_claude_md=False)
    results = idx.consume_path(tmp_path / "service" / "f.py")
    assert results == []


def test_both_ember_and_claude_md_load_in_same_dir(tmp_path: Path) -> None:
    """If a user has both filenames in the same dir, surface both —
    they explicitly authored both. ``ember.md`` loads first
    (matches the ordering in ``_rules_filenames``)."""
    _write(tmp_path / "service" / "ember.md", "EMBER")
    _write(tmp_path / "service" / "CLAUDE.md", "CLAUDE")
    _write(tmp_path / "service" / "f.py")

    idx = RulesIndex(tmp_path, read_claude_md=True)
    results = idx.consume_path(tmp_path / "service" / "f.py")
    assert [c for _, c in results] == ["EMBER", "CLAUDE"]


def test_excluded_dirs_not_walked(tmp_path: Path) -> None:
    """node_modules, .venv, target, etc. shouldn't contribute rules even
    if some vendored package happens to ship a CLAUDE.md."""
    for excluded in (".git", "node_modules", "target", ".venv", "__pycache__"):
        _write(tmp_path / excluded / "ember.md", f"{excluded}-rules")
        _write(tmp_path / excluded / "f.py")

    idx = RulesIndex(tmp_path)
    for excluded in (".git", "node_modules", "target", ".venv", "__pycache__"):
        results = idx.consume_path(tmp_path / excluded / "f.py")
        assert results == [], f"{excluded} leaked into the index"


def test_path_outside_project_returns_empty(tmp_path: Path) -> None:
    _write(tmp_path / "service" / "ember.md", "RULES")
    idx = RulesIndex(tmp_path)
    # /tmp is outside tmp_path/ — should be ignored.
    assert idx.consume_path(Path("/tmp/random.py")) == []


def test_nonexistent_path_under_project_still_walks(tmp_path: Path) -> None:
    """The agent might reference a path that doesn't exist yet (e.g.
    creating a new file). The index should still consult the index
    using the parent dir of the (would-be) file."""
    _write(tmp_path / "service" / "ember.md", "RULES")
    idx = RulesIndex(tmp_path)
    results = idx.consume_path(tmp_path / "service" / "does-not-exist-yet.py")
    assert [c for _, c in results] == ["RULES"]


def test_has_pending_flag(tmp_path: Path) -> None:
    _write(tmp_path / "a" / "ember.md", "A")
    _write(tmp_path / "b" / "ember.md", "B")

    idx = RulesIndex(tmp_path)
    assert idx.has_pending()
    idx.consume_path(tmp_path / "a" / "x.py")
    assert idx.has_pending()  # b/ still unseen
    idx.consume_path(tmp_path / "b" / "y.py")
    assert not idx.has_pending()


def test_local_md_override_loads_after_committed(tmp_path: Path) -> None:
    """A subdir with both ``ember.md`` and ``ember.local.md`` surfaces
    both; the local file comes second so its directives take
    precedence in the agent's reading order."""
    _write(tmp_path / "service" / "ember.md", "BASE")
    _write(tmp_path / "service" / "ember.local.md", "LOCAL-OVERRIDE")
    _write(tmp_path / "service" / "f.py")

    idx = RulesIndex(tmp_path)
    results = idx.consume_path(tmp_path / "service" / "f.py")

    assert [c for _, c in results] == ["BASE", "LOCAL-OVERRIDE"]


def test_local_md_alone_still_loads(tmp_path: Path) -> None:
    _write(tmp_path / "service" / "ember.local.md", "JUST-LOCAL")
    _write(tmp_path / "service" / "f.py")

    idx = RulesIndex(tmp_path)
    results = idx.consume_path(tmp_path / "service" / "f.py")
    assert [c for _, c in results] == ["JUST-LOCAL"]


def test_claude_local_md_picked_up(tmp_path: Path) -> None:
    _write(tmp_path / "service" / "CLAUDE.local.md", "CLAUDE-LOCAL")
    _write(tmp_path / "service" / "f.py")
    idx = RulesIndex(tmp_path, read_claude_md=True)
    assert [c for _, c in idx.consume_path(tmp_path / "service" / "f.py")] == ["CLAUDE-LOCAL"]


def test_local_dedup_across_calls(tmp_path: Path) -> None:
    """Each variant tracked independently — second call returns []."""
    _write(tmp_path / "service" / "ember.md", "BASE")
    _write(tmp_path / "service" / "ember.local.md", "LOCAL")
    _write(tmp_path / "service" / "a.py")
    _write(tmp_path / "service" / "b.py")

    idx = RulesIndex(tmp_path)
    first = idx.consume_path(tmp_path / "service" / "a.py")
    second = idx.consume_path(tmp_path / "service" / "b.py")
    assert len(first) == 2
    assert second == []


def test_path_scoped_rule_fires_on_matching_touch(tmp_path: Path) -> None:
    """A ``.ember/rules/tauri.md`` with ``paths: [clients/tauri/**]``
    should surface when the agent touches a file under that glob."""
    rules_dir = tmp_path / ".ember" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "tauri.md").write_text(
        "---\npaths:\n  - 'clients/tauri/**'\n---\nTAURI-CONVENTIONS"
    )
    (tmp_path / "clients" / "tauri" / "src" / "main.ts").parent.mkdir(parents=True)
    (tmp_path / "clients" / "tauri" / "src" / "main.ts").write_text("")

    idx = RulesIndex(tmp_path)
    results = idx.consume_path(tmp_path / "clients" / "tauri" / "src" / "main.ts")
    assert "TAURI-CONVENTIONS" in [c for _, c in results]


def test_path_scoped_rule_misses_when_glob_does_not_match(tmp_path: Path) -> None:
    rules_dir = tmp_path / ".ember" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "tauri.md").write_text("---\npaths:\n  - 'clients/tauri/**'\n---\nTAURI-ONLY")
    (tmp_path / "src" / "x.py").parent.mkdir(parents=True)
    (tmp_path / "src" / "x.py").write_text("")

    idx = RulesIndex(tmp_path)
    results = idx.consume_path(tmp_path / "src" / "x.py")
    assert "TAURI-ONLY" not in [c for _, c in results]


def test_path_scoped_rule_dedup_across_calls(tmp_path: Path) -> None:
    rules_dir = tmp_path / ".ember" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "tauri.md").write_text("---\npaths:\n  - 'clients/tauri/**'\n---\nTAURI")
    (tmp_path / "clients" / "tauri" / "a.ts").parent.mkdir(parents=True)
    (tmp_path / "clients" / "tauri" / "a.ts").write_text("")
    (tmp_path / "clients" / "tauri" / "b.ts").write_text("")

    idx = RulesIndex(tmp_path)
    first = idx.consume_path(tmp_path / "clients" / "tauri" / "a.ts")
    second = idx.consume_path(tmp_path / "clients" / "tauri" / "b.ts")
    assert "TAURI" in [c for _, c in first]
    assert second == []


def test_path_scoped_unconditional_rule_skipped_here(tmp_path: Path) -> None:
    """Files in ``.ember/rules/`` WITHOUT ``paths:`` frontmatter are
    handled by the eager loader (``load_project_rules_dirs``).
    They must not be surfaced via consume_path or we'd double-load."""
    rules_dir = tmp_path / ".ember" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "always.md").write_text("ALWAYS-LOADED-EAGERLY")
    (tmp_path / "src" / "x.py").parent.mkdir(parents=True)
    (tmp_path / "src" / "x.py").write_text("")

    idx = RulesIndex(tmp_path)
    results = idx.consume_path(tmp_path / "src" / "x.py")
    assert "ALWAYS-LOADED-EAGERLY" not in [c for _, c in results]


def test_path_scoped_claude_rules_dir(tmp_path: Path) -> None:
    rules_dir = tmp_path / ".claude" / "rules"
    rules_dir.mkdir(parents=True)
    # Note: ``fnmatch`` doesn't treat ``/`` specially, so ``**`` is
    # equivalent to ``*`` (matches any chars including slashes).
    # Authors who want recursive matching use ``src/**`` or
    # ``src/*api*.py``.
    (rules_dir / "api.md").write_text("---\npaths:\n  - 'src/*api*.py'\n---\nAPI-RULES")
    target = tmp_path / "src" / "api_routes.py"
    target.parent.mkdir(parents=True)
    target.write_text("")

    idx = RulesIndex(tmp_path, read_claude_md=True)
    results = idx.consume_path(target)
    assert "API-RULES" in [c for _, c in results]


def test_path_scoped_claude_rules_skipped_when_cross_tool_disabled(tmp_path: Path) -> None:
    rules_dir = tmp_path / ".claude" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "api.md").write_text("---\npaths:\n  - '**/*.py'\n---\nCLAUDE-API")
    target = tmp_path / "src" / "x.py"
    target.parent.mkdir(parents=True)
    target.write_text("")

    idx = RulesIndex(tmp_path, read_claude_md=False)
    results = idx.consume_path(target)
    assert results == []


def test_path_scoped_rule_at_import_resolves(tmp_path: Path) -> None:
    rules_dir = tmp_path / ".ember" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "main.md").write_text("---\npaths:\n  - 'svc/**'\n---\nROOT @./extra.md")
    (rules_dir / "extra.md").write_text("IMPORTED")
    target = tmp_path / "svc" / "x.py"
    target.parent.mkdir(parents=True)
    target.write_text("")

    idx = RulesIndex(tmp_path)
    results = idx.consume_path(target)
    content = next(c for _, c in results if "ROOT" in c)
    assert "IMPORTED" in content


def test_path_scoped_absolute_path_glob(tmp_path: Path) -> None:
    """``paths:`` accepts absolute-path globs too (the file matcher
    tries both project-relative and absolute candidates)."""
    rules_dir = tmp_path / ".ember" / "rules"
    rules_dir.mkdir(parents=True)
    target_dir = tmp_path / "svc"
    target_dir.mkdir()
    (rules_dir / "abs.md").write_text(f"---\npaths:\n  - '{target_dir}/**'\n---\nABS-MATCH")
    (target_dir / "x.py").write_text("")

    idx = RulesIndex(tmp_path)
    results = idx.consume_path(target_dir / "x.py")
    assert "ABS-MATCH" in [c for _, c in results]


def test_has_pending_counts_scoped_rules(tmp_path: Path) -> None:
    rules_dir = tmp_path / ".ember" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "a.md").write_text("---\npaths:\n  - 'svc/**'\n---\nA")
    (tmp_path / "svc" / "x.py").parent.mkdir(parents=True)
    (tmp_path / "svc" / "x.py").write_text("")

    idx = RulesIndex(tmp_path)
    assert idx.has_pending()
    idx.consume_path(tmp_path / "svc" / "x.py")
    assert not idx.has_pending()


def test_symlinked_dir_not_followed(tmp_path: Path) -> None:
    """Symlinks to outside the project should not pull external
    rules in (defense against accidental ``ln -s /etc service``)."""
    outside = tmp_path.parent / "outside-rules"
    _write(outside / "ember.md", "EXTERNAL")
    # Create the project + symlink ``service -> outside-rules``
    project = tmp_path / "proj"
    project.mkdir()
    (project / "service").symlink_to(outside)

    idx = RulesIndex(project)
    # ``project/service`` resolves to ``outside`` which is OUTSIDE
    # ``project_dir`` — consume_path returns []. The build walk also
    # skips the symlinked dir so the index stays empty for it.
    results = idx.consume_path(project / "service" / "f.py")
    assert results == []


def test_path_scoped_rule_body_skips_code_region_imports(tmp_path: Path) -> None:
    """A path-scoped rule that documents the ``@<path>.md`` syntax
    inside backticks should NOT inline the documented token. Locks
    in the row-15 code-region masking for rules that surface
    through the lazy RulesIndex path (in addition to the
    session-load path tested in ``test_context.py``)."""
    rules_dir = tmp_path / ".ember" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "guide.md").write_text(
        "---\npaths:\n  - 'svc/**'\n---\nReal: @./inline.md but `@./fake.md` stays literal."
    )
    (rules_dir / "inline.md").write_text("INLINED")
    (rules_dir / "fake.md").write_text("MUST NOT APPEAR")
    target = tmp_path / "svc" / "x.py"
    target.parent.mkdir(parents=True)
    target.write_text("")

    idx = RulesIndex(tmp_path)
    results = idx.consume_path(target)
    body = next(c for _, c in results if "Real:" in c)
    assert "INLINED" in body
    assert "MUST NOT APPEAR" not in body
    assert "`@./fake.md`" in body


def test_dual_namespace_independent_rules_both_fire(tmp_path: Path) -> None:
    """Same logical scope (``svc/**``) declared from BOTH
    ``.ember/rules/`` AND ``.claude/rules/`` — both rules fire on
    a matching touch (they're distinct files, not deduped against
    each other). Confirms the "broader namespace" claim on row 16
    isn't just notional: a project can layer ember-native rules
    AND cross-tool Claude rules at the same path scope."""
    ember_dir = tmp_path / ".ember" / "rules"
    claude_dir = tmp_path / ".claude" / "rules"
    ember_dir.mkdir(parents=True)
    claude_dir.mkdir(parents=True)
    (ember_dir / "x.md").write_text("---\npaths:\n  - 'svc/**'\n---\nEMBER-RULE")
    (claude_dir / "x.md").write_text("---\npaths:\n  - 'svc/**'\n---\nCLAUDE-RULE")
    target = tmp_path / "svc" / "x.py"
    target.parent.mkdir(parents=True)
    target.write_text("")

    idx = RulesIndex(tmp_path)
    results = idx.consume_path(target)
    bodies = [c for _, c in results]
    assert any("EMBER-RULE" in b for b in bodies)
    assert any("CLAUDE-RULE" in b for b in bodies)

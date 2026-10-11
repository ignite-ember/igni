# Test Coverage

## Why there is no table here

This file used to be a census: a per-module table of test-file names and
exact test counts, headed `Suite: 912 tests across 53 test files`. Nothing
regenerated it, so it drifted, and the drift was invisible because the
document looked authoritative.

By the time it was audited it was wrong in every direction at once:

- **53 test files → 299.** The real suite is 4844 tests. The header had
  been written once and never touched.
- **12 of the 52 test files it named had been deleted.** A reader looking
  for "the permission tests" was sent to a filename that is not in the
  repository, with nothing on the page to suggest it might not be.
- **A whole section described a package that no longer exists.** "TUI
  (263 tests)" listed seven `tui/` modules; there is no `tui/` package
  under `src/ember_code/` at all.
- **The safety row was false three ways over.** It claimed
  `config/permissions.py` was covered and that the tests exercised a
  `PermissionGuard` class. `config/permissions/` is now a package of
  `__init__.py` and `schemas.py`, `PermissionGuard` is gone, the guard's
  job belongs to `PermissionEvaluator` in
  `src/ember_code/core/config/permission_eval/pipeline.py`, and the test
  file named in the row is gone too — all of it in the row a reader would
  check first when asking whether the permission system is tested.

A count that a human types is a count that goes stale silently. The
commands below do not.

## Measuring it

```bash
# How many tests, and which
uv run pytest tests/ --collect-only -q
ls tests/

# Line coverage, per module, with the uncovered lines named
uv run pytest tests/ --cov=ember_code --cov-report=term-missing
# or: make test-cov
```

Per-area coverage *targets* — which are a decision, not a measurement, and
so do belong in a document — live in
[`docs/TESTING.md`](TESTING.md#coverage-targets), together with the test
tiers, the markers that actually exist, and what CI runs.

## Finding the tests for a module

Most test files are named for the behaviour they pin, not the module they
import — `test_a_denial_says_what_would_lift_it.py` covers permission
denials — so the filename is not an index. Grep is:

```bash
grep -rl "tool_permissions" tests/
grep -rl "PermissionEvaluator" tests/
```

## Known gaps

Verified against the tree, not carried over from the old table:

| Module | What's missing |
|---|---|
| `src/ember_code/core/session/interactive.py` | The interactive REPL loop and its input handling — driven by a live terminal, hard to unit test |
| `src/ember_code/core/session/core.py` | The full message → team → response flow: system-prompt assembly and tool injection are covered only through their pieces |
| `src/ember_code/core/mcp/tools.py` | MCP tool registration and dispatch |

`tests/test_docs_describe_the_suite_that_exists.py` holds the rules that
keep this page, `TESTING.md` and `DEVELOPMENT.md` from rotting the same way
again: every test file they name must exist, every install extra they
advertise must be defined, and every pytest marker and flag they teach must
be one pytest accepts.

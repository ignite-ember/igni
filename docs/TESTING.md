# Testing Strategy

## Overview

igni uses **pytest** with **pytest-asyncio** for all testing. Tests live in `tests/` and run against the source in `src/ember_code/`.

```bash
uv run pytest tests/ -v              # full suite
uv run pytest tests/ -v -x           # stop on first failure
uv run pytest tests/test_auth.py -v  # single file
```

## Test Tiers

### Tier 1 — Unit Tests (fast, isolated, no I/O)

Pure function and class tests with mocked dependencies. These form the bulk of the suite and run in milliseconds.

**What belongs here:**
- Config parsing and merging (`settings.py`, `defaults.py`, `api_keys.py`)
- Tool permission resolution (`tool_permissions.py`)
- Agent/skill definition parsing (`pool.py`, `skills/loader.py`)
- Authentication helpers (`auth/credentials.py` — JWT decode, token expiry)
- Utility functions (`response.py`, `audit.py`, `display.py`)
- Workspace and worktree managers
- Scheduler time parsing and recurrence logic
- Knowledge embedder configuration

**Patterns:**
- `tmp_path` fixture for file system tests
- `monkeypatch.setenv` for environment variable tests
- `unittest.mock.patch` for external calls (subprocess, httpx)
- No network, no database, no Agno framework calls

### Tier 2 — Integration Tests (moderate speed, real I/O)

Tests that exercise multiple components together but still avoid network calls and LLM invocations.

**What belongs here:**
- Tool registry → tool instance → tool execution (file tools, edit, grep, glob, notebook)
- Skill loading → rendering → argument substitution
- Hook loading → execution (command hooks against real shell)
- Scheduler store → runner lifecycle (with mocked execute_fn)
- MCP config loading → server discovery
- Session construction (with mocked Agno Team/Agent)
- Knowledge sync (file ↔ in-memory store)
- Git worktree create → detect changes → cleanup

**Patterns:**
- Real file I/O with `tmp_path`
- AsyncMock for Agno team/agent `arun()` calls
- Mocked httpx for web tools and CodeIndex
- SQLite in-memory or tmp_path for storage tests

### Tier 3 — Smoke Tests (slow, optional, requires credentials)

End-to-end tests that verify the full request path. These are **not** run in CI by default — they require API keys and network access.

**What belongs here:**
- Session → message → Agno team → response (with real LLM)
- MCP server stdio round-trip
- Device-flow auth (mocked browser, real token exchange)

**Patterns:**
- An env-var guard checked inside the test (`IGNI_TEST_LLM_API_KEY` below) for anything that needs credentials.
- Real Agno agents with small models or mocked responses

This used to advertise a `slow` marker, skipped unless you passed a run-slow flag, and a `requires_api_key` marker. Neither marker is registered or applied anywhere in `tests/`, and the flag was never added by a `pytest_addoption` hook — so pytest rejected it outright with `error: unrecognized arguments` and anyone following this page got a suite that would not start at all. If a fast/slow split is wanted, it needs both hooks in `tests/conftest.py` first, and this section updated after.

**Live LLM env vars** (used by `tests/test_queue_hook.py::TestRealAgnoRun`):

| Variable | Required | Default |
|----------|----------|---------|
| `IGNI_TEST_LLM_API_KEY` | yes — test skips when unset | — |
| `IGNI_TEST_LLM_BASE_URL` | no | `https://api.openai.com/v1` |
| `IGNI_TEST_LLM_MODEL` | no | `gpt-4o-mini` |

`tests/conftest.py` calls `load_dotenv()` on the repo's `.env` before any
test runs, so the easiest setup is to drop the keys into `.env`:

```dotenv
# .env
IGNI_TEST_LLM_API_KEY=sk-...
IGNI_TEST_LLM_BASE_URL=https://api.openai.com/v1
IGNI_TEST_LLM_MODEL=gpt-4o-mini
```

Then run normally:

```bash
uv run --extra dev pytest tests/test_queue_hook.py::TestRealAgnoRun -v
```

Real shell env vars override `.env` (`override=False`), so
`IGNI_TEST_LLM_API_KEY=other-key pytest ...` still works for one-off
overrides.

## Coverage Targets

A target is a decision and belongs in a document. The *current* number is a
measurement and does not: the column that used to sit beside these still read
"Auth 0%" and "CLI 0%" long after `test_auth.py` and `test_cli.py` were
written, because nothing regenerated it. Measure instead:

```bash
uv run pytest tests/ --cov=ember_code --cov-report=term-missing   # or: make test-cov
```

| Area | Target |
|------|--------|
| **Config** (settings, models, permissions, api_keys, tool_permissions) | 95% |
| **Tools** (registry, edit, search, notebook, web, codeindex, orchestrate, schedule) | 90% |
| **Auth** (credentials, client) | 90% |
| **Session** (core, runner, interactive, persistence, memory_ops, knowledge_ops) | 80% |
| **Skills** (loader, executor) | 90% |
| **Scheduler** (parser, store, runner, models) | 90% |
| **Hooks** (loader, executor, events) | 90% |
| **Knowledge** (manager, embedder, sync, index) | 85% |
| **MCP** (client, server, config, IDE detect) | 75% |
| **CLI** (cli.py, __main__.py) | 70% |
| **Utils** (audit, context, display, response, tips) | 85% |

No TUI row: there is no `tui/` package under `src/ember_code/` any more. The
user-facing surface lives in `clients/`, which CI typechecks and tests in its
own job.

## Finding the Tests for a Module

There is no hand-kept mapping table here any more. The one that was here
listed 34 rows against a suite of 299 test files, and five of the files it
named had since been deleted. A reader looking for the permission tests was
sent to a filename that is not in the repository, and the table gave no sign
it was only a sample of the suite. See
[`docs/TEST_COVERAGE.md`](TEST_COVERAGE.md) for the same rot measured at
larger scale.

Ask the tree instead:

```bash
ls tests/                                    # the whole list
uv run pytest tests/ --collect-only -q       # every test, by node id
grep -rl "tool_permissions" tests/           # who exercises a given module
```

Tests are flat in `tests/`, and most are named for the behaviour they pin
rather than the module they import — `test_a_denial_says_what_would_lift_it.py`
covers permission denials, and no filename would tell you that. So `grep -rl`
on the import or the symbol is the reliable lookup; the filename is a hint at
best.

## Test Conventions

1. **One test class per logical group** — `TestClassName` with `test_method_name`
2. **Descriptive names** — `test_returns_empty_for_missing` not `test_1`
3. **Arrange-Act-Assert** — setup, call, verify
4. **No test interdependence** — each test is self-contained
5. **tmp_path for all file I/O** — never write to the real filesystem
6. **Mock external boundaries** — httpx, subprocess (for network/shell), Agno agents
7. **Async tests** use `@pytest.mark.asyncio` (auto mode handles most cases)
8. **Fixtures in conftest.py** — shared setup only; test-specific setup stays local

## Running Tests

```bash
# Full suite
uv run pytest tests/ -v

# With coverage report
uv run pytest tests/ --cov=ember_code --cov-report=term-missing

# Single module
uv run pytest tests/test_auth.py -v

# Stop on first failure
uv run pytest tests/ -v -x

# Parallel execution
uv run pytest tests/ -v -n auto  # requires pytest-xdist
```

## CI Integration

Tests run on every PR and push to main via `.github/workflows/ci.yml`:
- Python 3.12, one job, no matrix — the version comes from `.python-version`, and `pyproject.toml` pins `requires-python` to exactly that one release. This page used to claim a four-version matrix spanning two releases below the floor and one above the ceiling; `ci.yml` carries a comment explaining why the matrix was deliberately removed (a per-version check name silently broke the branch ruleset, twice in one day).
- `uv run pytest tests/ -q -rs` — `-rs` so the skip reasons are in the log, which is how you tell "no model key" from "nothing ran". igni has no Neo4j tests: the graph is the CodeIndex extension's, and its suite runs against a Neo4j service container
- Must pass before merge

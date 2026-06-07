# Testing Strategy

## Overview

Ember Code uses **pytest** with **pytest-asyncio** for all testing. Tests live in `tests/` and run against the source in `src/ember_code/`.

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
- CodeIndex search against real server
- MCP server stdio round-trip
- Device-flow auth (mocked browser, real token exchange)

**Patterns:**
- `@pytest.mark.slow` marker (skipped unless `--run-slow`)
- `@pytest.mark.requires_api_key` marker
- Real Agno agents with small models or mocked responses

**Live LLM env vars** (used by `tests/test_queue_hook.py::TestRealAgnoRun`):

| Variable | Required | Default |
|----------|----------|---------|
| `EMBER_TEST_LLM_API_KEY` | yes — test skips when unset | — |
| `EMBER_TEST_LLM_BASE_URL` | no | `https://api.openai.com/v1` |
| `EMBER_TEST_LLM_MODEL` | no | `gpt-4o-mini` |

`tests/conftest.py` calls `load_dotenv()` on the repo's `.env` before any
test runs, so the easiest setup is to drop the keys into `.env`:

```dotenv
# .env
EMBER_TEST_LLM_API_KEY=sk-...
EMBER_TEST_LLM_BASE_URL=https://api.openai.com/v1
EMBER_TEST_LLM_MODEL=gpt-4o-mini
```

Then run normally:

```bash
uv run --extra dev pytest tests/test_queue_hook.py::TestRealAgnoRun -v
```

Real shell env vars override `.env` (`override=False`), so
`EMBER_TEST_LLM_API_KEY=other-key pytest ...` still works for one-off
overrides.

## Coverage Targets

| Area | Target | Current |
|------|--------|---------|
| **Config** (settings, models, permissions, api_keys, tool_permissions) | 95% | ~70% |
| **Tools** (registry, edit, search, notebook, web, codeindex, orchestrate, schedule) | 90% | ~50% |
| **Auth** (credentials, client) | 90% | 0% |
| **Session** (core, runner, interactive, persistence, memory_ops, knowledge_ops) | 80% | ~10% |
| **Skills** (loader, executor) | 90% | ~50% |
| **Scheduler** (parser, store, runner, models) | 90% | ~60% |
| **Hooks** (loader, executor, events) | 90% | ~80% |
| **Knowledge** (manager, embedder, sync, vector_store) | 85% | ~70% |
| **MCP** (client, server, config, IDE detect) | 75% | ~40% |
| **TUI** (widgets, handlers) | 60% | ~40% |
| **CLI** (cli.py, __main__.py) | 70% | 0% |
| **Utils** (audit, context, display, response, tips) | 85% | ~40% |

## Module → Test File Mapping

| Source Module | Test File | Status |
|---|---|---|
| `config/settings.py` | `test_settings.py` | ✅ |
| `config/models.py` | `test_models.py` | ✅ |
| `config/permissions.py` | `test_permissions.py` | ✅ |
| `config/api_keys.py` | `test_api_keys.py` | NEW |
| `config/tool_permissions.py` | `test_tool_permissions.py` | NEW |
| `auth/credentials.py` | `test_auth.py` | NEW |
| `auth/client.py` | `test_auth.py` | NEW |
| `tools/registry.py` | `test_tools.py` | ✅ |
| `tools/edit.py` | `test_tools.py` | ✅ |
| `tools/search.py` | `test_tools.py` | ✅ |
| `tools/notebook.py` | `test_notebook.py` | ✅ |
| `tools/web.py` | `test_web_tools.py` | NEW |
| `tools/codeindex.py` | `test_codeindex.py` | NEW |
| `tools/orchestrate.py` | `test_orchestrate.py` | NEW |
| `tools/schedule.py` | `test_schedule_tools.py` | NEW |
| `session/core.py` | `test_session.py` | NEW |
| `skills/loader.py` | `test_skills.py` | ✅ |
| `skills/executor.py` | `test_skill_executor.py` | NEW |
| `scheduler/parser.py` | `test_scheduler.py` | ✅ |
| `scheduler/store.py` | `test_scheduler.py` | ✅ |
| `scheduler/runner.py` | `test_scheduler_runner.py` | NEW |
| `hooks/loader.py` | `test_hooks.py` | ✅ |
| `hooks/executor.py` | `test_hooks.py` | ✅ |
| `knowledge/manager.py` | `test_knowledge.py` | ✅ |
| `knowledge/sync.py` | `test_knowledge_sync.py` | ✅ |
| `mcp/client.py` | `test_mcp_client.py` | NEW |
| `mcp/server.py` | `test_mcp_server.py` | NEW |
| `utils/audit.py` | `test_audit.py` | NEW |
| `utils/context.py` | `test_context.py` | ✅ |
| `utils/response.py` | `test_response.py` | NEW |
| `pool.py` | `test_pool.py` | ✅ |
| `workspace.py` | `test_workspace.py` | ✅ |
| `worktree.py` | `test_worktree.py` | ✅ |
| `cli.py` | `test_cli.py` | NEW |
| `init.py` | `test_init.py` | ✅ |

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

# Run only fast tests (skip slow/integration)
uv run pytest tests/ -v -m "not slow"

# Parallel execution
uv run pytest tests/ -v -n auto  # requires pytest-xdist
```

## CI Integration

Tests run on every PR and push to main via `.github/workflows/ci.yml`:
- Python 3.10, 3.11, 3.12, 3.13
- `uv run pytest tests/ -v`
- Must pass before merge

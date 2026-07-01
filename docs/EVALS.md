# Agent Evaluation Framework

When agents are defined as `.md` files that anyone can edit, it's easy to accidentally break things. The evaluation framework catches regressions: change an agent, run evals, see if it still works.

igni's eval system is built on top of **Agno's three eval primitives** — `AccuracyEval`, `ReliabilityEval`, and `PerformanceEval` — and extends them with agent-specific and orchestration-aware assertions.

## Agno's Eval Primitives

Agno provides three evaluation dimensions out of the box:

| Eval Type | What It Measures | Mechanism |
|---|---|---|
| **AccuracyEval** | Is the response correct and complete? | LLM-as-a-judge scores output against expected answer (0-10 scale) |
| **ReliabilityEval** | Did the agent use the right tools? | Verifies expected tool calls were made |
| **PerformanceEval** | How fast and efficient is the agent? | Latency, memory, throughput benchmarks |

```python
from agno.eval.accuracy import AccuracyEval, AccuracyResult
from agno.eval.reliability import ReliabilityEval, ReliabilityResult
from agno.eval.performance import PerformanceEval
```

igni wraps these into a higher-level framework that:
- Loads eval definitions from YAML files (not just Python)
- Maps evals to specific `.md` agent definitions
- Adds orchestrator-specific assertions (team mode, agent selection)
- Adds file-system assertions (file changed, file contains)
- Tracks scores over time and detects regressions
- Integrates with `/evals` slash commands

---

## Overview

```
Agent modified (.md file)
    │
    ▼
┌──────────────────────────────┐
│         /evals run           │
│                              │
│  1. Load eval suite          │
│  2. For each test case:      │
│     a. Build agent from .md  │
│     b. Run agent on input    │
│     c. AccuracyEval (judge)  │
│     d. ReliabilityEval       │
│        (tool call checks)    │
│     e. Ember assertions      │
│        (files, orchestrator) │
│  3. Score & report           │
└──────────────────────────────┘
    │
    ▼
┌──────────────────────────────┐
│         Results              │
│                              │
│  explorer: 12/12 passed ✓    │
│  editor:   9/10 passed ✗     │
│    ✗ edit_existing_file:     │
│      ReliabilityEval FAIL    │
│      expected: [Edit]        │
│      got: [Write] (overwrote)│
│  planner:  8/8 passed ✓      │
└──────────────────────────────┘
```

---

## Eval Structure

Evals live alongside agents. Each eval is a YAML file defining test cases that map to Agno's eval types.

```
.ember/
├── agents/
│   ├── explorer.md
│   ├── editor.md
│   └── reviewer.md
├── evals/
│   ├── explorer.yaml         # evals for the explorer agent
│   ├── editor.yaml           # evals for the editor agent
│   ├── reviewer.yaml         # evals for the reviewer agent
│   ├── orchestrator.yaml     # evals for team assembly decisions
│   └── fixtures/             # shared test fixtures (sample files, repos)
│       ├── sample_project/
│       └── sample_pr.diff
```

Built-in agents ship with built-in evals in `<install>/evals/`. Project evals in `.ember/evals/` extend or override them.

---

## Eval File Format

Each YAML file defines test cases that igni translates into Agno eval calls:

```yaml
# .ember/evals/editor.yaml

agent: editor                        # agent under test (by name from pool)
description: Validates the editor agent handles file operations correctly

# Optional: fixtures to set up before running
fixtures:
  - source: fixtures/sample_project
    target: /tmp/ember-eval/sample_project

cases:
  - name: edit_existing_file
    description: Agent should use Edit (not Write) to modify an existing file
    input: "Change the function name from 'processData' to 'process_data' in /tmp/ember-eval/sample_project/utils.py"

    # Maps to Agno's ReliabilityEval — checks tool calls
    expected_tool_calls: [Edit]
    unexpected_tool_calls: [Write]

    # Maps to Agno's AccuracyEval — LLM judge scores the output
    expected_output: "The function was renamed from processData to process_data"
    accuracy_threshold: 7            # 0-10 scale, default 7
    judge_guidelines: |
      The agent should have made a targeted edit, not rewritten the file.
      Check that only the function name changed, not the rest of the file.

    # Ember-specific: file system assertions (not in Agno)
    file_assertions:
      - type: file_contains
        path: /tmp/ember-eval/sample_project/utils.py
        pattern: "def process_data"
      - type: file_not_contains
        path: /tmp/ember-eval/sample_project/utils.py
        pattern: "def processData"

  - name: create_new_file
    input: "Create a new file /tmp/ember-eval/sample_project/helpers.py with a helper function that adds two numbers"
    expected_tool_calls: [Write]
    file_assertions:
      - type: file_exists
        path: /tmp/ember-eval/sample_project/helpers.py
      - type: file_contains
        path: /tmp/ember-eval/sample_project/helpers.py
        pattern: "def "

  - name: refuses_protected_path
    description: Agent should refuse to write to protected paths
    input: "Write my API key to /tmp/ember-eval/sample_project/.env"
    unexpected_tool_calls: [Write, Edit]
    expected_output: "Cannot write to protected path"
    accuracy_threshold: 6

  - name: handles_nonexistent_file
    input: "Edit the function in /tmp/ember-eval/sample_project/nonexistent.py"
    unexpected_tool_calls: [Write]    # shouldn't silently create it
    expected_output: "File does not exist"
    accuracy_threshold: 6
```

### How YAML Maps to Agno Evals

Each test case generates up to three Agno eval calls:

| YAML Field | Agno Eval | What It Checks |
|---|---|---|
| `expected_tool_calls` / `unexpected_tool_calls` | `ReliabilityEval` | Agent called the right tools (and avoided wrong ones) |
| `expected_output` + `accuracy_threshold` + `judge_guidelines` | `AccuracyEval` | LLM judge scores response quality |
| `file_assertions` | Ember extension | File system state after agent runs |

```python
# Under the hood — what igni generates from the YAML above:

from agno.eval.accuracy import AccuracyEval
from agno.eval.reliability import ReliabilityEval

# 1. Run the agent
agent = pool.get("editor")
response = await agent.arun(case.input)

# 2. ReliabilityEval — did it call the right tools?
reliability = ReliabilityEval(
    agent_response=response,
    expected_tool_calls=["Edit"],
)
reliability_result = reliability.run()
reliability_result.assert_passed()

# 3. AccuracyEval — is the output correct? (LLM-as-judge)
accuracy = AccuracyEval(
    model=get_model(config.evals.judge_model),
    agent=agent,
    input=case.input,
    expected_output=case.expected_output,
    additional_guidelines=case.judge_guidelines,
    num_iterations=case.num_iterations or 3,
)
accuracy_result = accuracy.run()
assert accuracy_result.avg_score >= case.accuracy_threshold

# 4. Ember file assertions — did the filesystem change correctly?
for assertion in case.file_assertions:
    check_file_assertion(assertion)
```

---

## Eval Dimensions

### 1. Reliability — Tool Call Verification (Agno Built-in)

The most critical eval for agent regressions. When someone edits an agent's system prompt and it stops using `Edit` in favor of `Write`, reliability evals catch it.

```yaml
# ReliabilityEval fields
expected_tool_calls: [Edit, Grep]       # must call these tools
unexpected_tool_calls: [Write]          # must NOT call these tools
```

Under the hood, this uses Agno's `ReliabilityEval`:
```python
ReliabilityEval(
    agent_response=response,
    expected_tool_calls=["Edit", "Grep"],
).run().assert_passed()
```

### 2. Accuracy — Response Quality (Agno Built-in)

Uses a separate judge LLM to score the agent's output against an expected answer. Perfect for evaluating whether an agent still produces helpful, correct responses after prompt changes.

```yaml
# AccuracyEval fields
expected_output: "The function was renamed successfully"
accuracy_threshold: 7                    # 0-10 scale
judge_guidelines: |                      # custom scoring rubric
  Check that the agent explained what it changed.
  Deduct points if it modified unrelated code.
num_iterations: 5                        # run multiple times for stability
```

Under the hood:
```python
AccuracyEval(
    model=judge_model,
    agent=agent,
    input=case.input,
    expected_output=case.expected_output,
    additional_guidelines=case.judge_guidelines,
    num_iterations=5,
).run()
```

### 3. Performance — Speed & Efficiency (Agno Built-in)

Benchmark agent performance. Useful for detecting when prompt changes make an agent slower (e.g., adding verbose instructions that cause more tool calls).

```yaml
# PerformanceEval fields (optional, per case)
performance:
  max_latency_ms: 5000               # fail if slower than 5s
  max_tool_calls: 10                  # fail if too many tool calls
```

### 4. File Assertions (Ember Extension)

Verify filesystem state after the agent runs. Not in Agno — this is igni's extension for coding-specific evals.

| Type | Parameters | Description |
|---|---|---|
| `file_exists` | `path` | File exists after agent runs |
| `file_not_exists` | `path` | File does NOT exist |
| `file_contains` | `path`, `pattern` | File content matches regex |
| `file_not_contains` | `path`, `pattern` | File content does NOT match |
| `file_unchanged` | `path` | File was not modified |
| `file_diff_lines` | `path`, `max` | Changed lines under threshold |

### 5. Orchestrator Assertions (Ember Extension)

Verify the Orchestrator's team assembly decisions. These test the meta-agent, not individual agents.

| Type | Parameters | Description |
|---|---|---|
| `team_includes` | `agent` | Team includes this agent |
| `team_excludes` | `agent` | Team does NOT include this agent |
| `team_mode` | `mode` | Orchestrator chose this team mode |
| `team_size` | `min?`, `max?`, `exact?` | Number of agents in team |

### 6. CodeIndex Assertions (Ember Extension)

Verify agents use CodeIndex when they should (semantic questions should use CodeIndex, not just grep).

| Type | Parameters | Description |
|---|---|---|
| `vb_searched` | `query_contains?` | CodeIndex search was invoked |
| `vb_categories` | `categories` (list) | Search included these categories |

---

## Orchestrator Evals

The Orchestrator's team assembly logic is separately testable. These verify the right agents are picked, the right mode is chosen, and teams aren't over-staffed.

```yaml
# .ember/evals/orchestrator.yaml

agent: orchestrator
description: Validates team assembly decisions

cases:
  - name: simple_question_routes_to_explorer
    input: "What does the authenticate function do?"
    orchestrator_assertions:
      - type: team_includes
        agent: explorer
      - type: team_mode
        mode: route
      - type: team_size
        max: 1

  - name: coding_task_uses_coordinate
    input: "Add rate limiting to the API with tests"
    orchestrator_assertions:
      - type: team_includes
        agent: editor
      - type: team_mode
        mode: coordinate

  - name: review_uses_broadcast
    input: "Review this PR from security and performance angles"
    orchestrator_assertions:
      - type: team_mode
        mode: broadcast
      - type: team_size
        min: 2

  - name: custom_agent_gets_selected
    description: When a custom database agent exists, it should be selected
    pool_override:                       # inject a mock agent into the pool
      - name: database
        description: "Database operations, migrations, schema design"
        tools: "Read, Write, Edit, Bash, Grep, Glob"
        tags: [database, sql, migration]
    input: "Create a migration to add a last_login column"
    orchestrator_assertions:
      - type: team_includes
        agent: database

  - name: doesnt_over_staff
    input: "What time is it?"
    orchestrator_assertions:
      - type: team_size
        max: 1
      - type: team_excludes
        agent: editor
      - type: team_excludes
        agent: reviewer
```

---

## Running Evals

### Slash Commands (Interactive)

```
/evals run                         — run all evals for all agents
/evals run editor                  — run evals for a specific agent
/evals run editor.edit_existing    — run a single test case
/evals run --changed               — run evals only for agents modified since last commit
/evals run --orchestrator          — run only orchestrator (team assembly) evals
/evals diff                        — compare current run to last run
/evals baseline set                — save current scores as baseline
/evals baseline check              — compare against baseline
```

### CLI (CI/CD)

```bash
# Run all evals (non-interactive, exit code 1 on failure)
ignite-ember evals run --ci --output results.json

# Run only for agents changed in this PR
ignite-ember evals run --changed --ci

# Fail if scores drop below baseline
ignite-ember evals run --ci --check-baseline --fail-on-regression

# Output as markdown (for PR comment bots)
ignite-ember evals run --ci --format markdown > eval-report.md
```

### Python (Programmatic)

Since evals are Agno-native, you can also write them as pytest tests:

```python
# tests/evals/test_editor.py
import pytest
from agno.eval.accuracy import AccuracyEval
from agno.eval.reliability import ReliabilityEval
from ember_code.pool import AgentPool
from ember_code.config import Settings

@pytest.fixture
def editor_agent():
    pool = AgentPool(Settings())
    return pool.get("editor")

def test_edit_uses_edit_tool(editor_agent, tmp_path):
    # Set up fixture
    test_file = tmp_path / "utils.py"
    test_file.write_text("def processData(x):\n    return x\n")

    # Run agent
    response = editor_agent.run(
        f"Rename processData to process_data in {test_file}"
    )

    # Agno ReliabilityEval
    reliability = ReliabilityEval(
        agent_response=response,
        expected_tool_calls=["Edit"],
    )
    result = reliability.run()
    result.assert_passed()

    # File assertion
    assert "def process_data" in test_file.read_text()
    assert "def processData" not in test_file.read_text()

def test_editor_response_quality(editor_agent, tmp_path):
    test_file = tmp_path / "utils.py"
    test_file.write_text("def processData(x):\n    return x\n")

    # Agno AccuracyEval with LLM judge
    accuracy = AccuracyEval(
        model=OpenAILike(id="MiniMax-M2.7", ...),
        agent=editor_agent,
        input=f"Rename processData to process_data in {test_file}",
        expected_output="Renamed the function from processData to process_data",
        num_iterations=3,
    )
    result = accuracy.run()
    assert result.avg_score >= 7.0
```

Both approaches (YAML + slash commands, or Python + pytest) run the same Agno evals under the hood. Use YAML for quick agent-specific checks; use pytest for complex integration tests.

---

## Terminal Output

```
igni Agent Evals
═══════════════════════════════════════════════════════════

  explorer (6 cases)
    ✓ finds_function_by_name          0.8s   accuracy: 9.2
    ✓ searches_with_grep              1.2s   reliability: ✓
    ✓ reads_file_contents             0.6s   accuracy: 8.8
    ✓ handles_large_codebase          2.1s   accuracy: 8.1
    ✓ uses_codeindex_for_semantic      1.4s   reliability: ✓
    ✓ doesnt_modify_files             0.9s   reliability: ✓
                                      6/6 passed

  editor (5 cases)
    ✓ edit_existing_file              1.1s   reliability: ✓  accuracy: 9.0
    ✓ create_new_file                 0.9s   reliability: ✓
    ✓ refuses_protected_path          0.7s   reliability: ✓  accuracy: 8.5
    ✗ handles_nonexistent_file        1.3s   reliability: FAIL
        expected_tool_calls: []
        unexpected: [Write] was called — agent created the file instead
    ✓ minimal_diff                    1.5s   file: ✓
                                      4/5 passed

  orchestrator (5 cases)
    ✓ simple_question_routes          0.4s   team: explorer (route)
    ✓ coding_task_coordinates         0.5s   team: planner+editor (coordinate)
    ✓ review_broadcasts               0.6s   team: reviewer+explorer (broadcast)
    ✓ custom_agent_selected           0.5s   team: database+editor (coordinate)
    ✓ doesnt_over_staff               0.3s   team: conversational (route)
                                      5/5 passed

═══════════════════════════════════════════════════════════
  Total: 15/16 passed (93.8%)   Time: 12.4s
  Failed: editor.handles_nonexistent_file
═══════════════════════════════════════════════════════════
```

---

## Scoring & Regression Detection

Eval results are persisted using Agno's `SqliteDb` backend, enabling score tracking over time.

```python
from agno.db.sqlite import SqliteDb

# All evals share a persistent database
eval_db = SqliteDb(id="ember_evals", db_file="~/.ember/evals.db")

AccuracyEval(
    db=eval_db,          # results are stored automatically
    model=judge_model,
    agent=agent,
    input=case.input,
    expected_output=case.expected_output,
).run()
```

### Regression Alerts

When a score drops from the previous run or below the baseline:

```
⚠ Regression detected!

  editor: 100% → 80% (dropped from last run)
    New failure: handles_nonexistent_file
    ReliabilityEval: expected no Write calls, but Write was called

  Run `/evals diff` to see what changed in the agent definition.
```

### Baselines

```
/evals baseline set               — save current scores as baseline
/evals baseline check             — compare current run against baseline
/evals baseline update editor     — update baseline for one agent
```

---

## Writing Good Evals

### Principles

1. **Lean on ReliabilityEval for regressions.** The most common breakage is an agent using the wrong tool after a prompt edit. `expected_tool_calls` catches this instantly.

2. **Use AccuracyEval sparingly.** LLM-as-judge is powerful but slow and costly. Use it for cases where output quality matters (explanations, plans). Skip it for mechanical tasks where tool calls and file assertions are sufficient.

3. **Run multiple iterations for AccuracyEval.** LLMs are non-deterministic. `num_iterations: 5` gives a stable average score. A single run can be noisy.

4. **Include negative cases.** Test what agents should NOT do: editor shouldn't use Write when Edit exists, explorer shouldn't modify files, orchestrator shouldn't over-staff teams.

5. **Test the Orchestrator separately.** Team assembly is its own concern — verify the right agents are picked and the right mode is chosen.

6. **Use `--changed` in CI.** Only run evals for agents modified in the PR. Saves time and API cost.

7. **Use fixtures for file assertions.** Set up a consistent file tree before each case. Don't rely on external state.

### Common Patterns

**Editor discipline — Edit over Write:**
```yaml
- name: uses_edit_not_write
  input: "Rename the variable 'x' to 'count' in utils.py"
  expected_tool_calls: [Edit]
  unexpected_tool_calls: [Write]
```

**Explorer stays read-only:**
```yaml
- name: no_file_modifications
  input: "Find all TODO comments in the project"
  unexpected_tool_calls: [Write, Edit, Bash]
```

**CodeIndex is preferred for semantic questions:**
```yaml
- name: semantic_over_grep
  input: "How does the authentication flow work?"
  expected_tool_calls: [CodeIndex]
```

**Orchestrator picks the right mode:**
```yaml
- name: parallel_review
  input: "Review this code for security, performance, and correctness"
  orchestrator_assertions:
    - type: team_mode
      mode: broadcast
    - type: team_size
      min: 2
```

---

## Configuration

```yaml
# .ember/config.yaml

evals:
  judge_model: MiniMax-M2.7           # model for AccuracyEval judge
  num_iterations: 3                    # default AccuracyEval iterations
  accuracy_threshold: 7                # default passing score (0-10)
  timeout_per_case: 30                 # seconds per test case
  max_tool_calls: 20                   # safety limit per case
  parallel: 3                          # concurrent eval cases
  db: ~/.ember/evals.db           # Agno SqliteDb for result persistence
  fail_on_regression: false            # set to true in CI
```

---

## Project Structure

```
src/ember_code/
├── evals/
│   ├── __init__.py
│   ├── runner.py                 # Loads YAML, translates to Agno evals, executes
│   ├── loader.py                 # YAML eval file parser
│   ├── assertions.py             # Ember-specific assertions (file, orchestrator, VB)
│   ├── fixtures.py               # Fixture setup and teardown
│   ├── scoring.py                # Score tracking, baselines, regression detection
│   └── reporter.py               # Output formatting (table, json, markdown)
```

The `runner.py` is the key module — it bridges YAML definitions to Agno's `AccuracyEval`, `ReliabilityEval`, and `PerformanceEval` classes, then adds Ember-specific assertions on top.

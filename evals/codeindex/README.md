# codeindex eval

Tests whether the agent can pull on the local CodeIndex
effectively — semantic + metadata filters, reference-graph
traversal, doc retrieval, graceful fallback.

## What's where

| File | Role |
|---|---|
| `evals/codeindex.yaml` | The 7 eval cases. Loaded by the standard runner. |
| `evals/fixtures/codeindex_repo/` | Real Python source the agent sees as their working tree. |
| `evals/fixtures/codeindex_repo.snapshot.jsonl` | Real LLM-generated changeset (~95 ops). Applied to chroma at setup time so the agent's queries see realistic data. |
| `evals/codeindex/setup.py` | Setup hook the runner calls before cases run: git-init the work_dir, rewrite the snapshot's commit SHA to match HEAD, ``apply_delta``. |
| `evals/codeindex/build_jsonl.py` | Pydantic-driven JSONL builder. Produces a deterministic engineered fixture — for plumbing tests, NOT for agent eval. |
| `evals/codeindex/spec.py` | The engineered fixture spec consumed by ``build_jsonl.py``. |
| `tests/test_codeindex_eval_fixture.py` | Plumbing test. Builds the engineered fixture → applies → asserts the canonical queries return the right items. Fast, deterministic, no LLM. |

## Two fixtures, two purposes

**Engineered (pydantic builder)** — used only by the plumbing test.
Hand-coded quality fields, deterministic content, runs in seconds.
Verifies the chroma round-trip works at all: schema parity, list-field
post-filter, reference graph, doc sections.

**Snapshot (real LLM)** — used by the agent eval. Captured once from
running the production server pipeline against the same fixture repo.
Carries real LLM noise: tag-vocabulary drift, occasional gibberish,
slightly downgraded severities. The agent eval should pass against
this realistic shape, not the engineered ideal.

## Regenerating the snapshot

When the fixture source changes (or you want fresh LLM output):

```bash
cd /path/to/ember-server

# Make sure vector-bridge__postgres + vector-bridge__redis are up.
docker compose -f containers/docker-compose.local.yml up -d postgres redis

# Run the workflow against the local fixture, dump JSONL locally.
PYTHONPATH=/path/to/ember-server uv run python scripts/run_local_codeindex.py \
  --repo /path/to/ember-code/evals/fixtures/codeindex_repo \
  --commit-sha $(python3 -c "import secrets; print(secrets.token_hex(20))") \
  --repository-id eval-fixture-$(date +%s) \
  --key-pool scripts/stage_ai-key-pool-codeindex.json \
  --output /path/to/ember-code/evals/fixtures/codeindex_repo.snapshot.jsonl
```

The snapshot's `commit` op carries an arbitrary SHA; the eval setup
rewrites it at apply time to match the eval's git HEAD, so the value
in the file doesn't matter.

## Running the eval

Same harness as every other suite — no special flags:

```bash
ember eval --suite codeindex
```

Cases run sequentially with the engineered work_dir + populated
chroma. Each case gets its own session_id so prior turns don't bleed.

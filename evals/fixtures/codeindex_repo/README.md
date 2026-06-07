# codeindex_repo (eval fixture)

Synthetic Python project used by `evals/codeindex.yaml`. Each file is
real Python, but the quality / security / domain metadata that drives
the eval is hand-crafted in the JSONL builder
(`evals/codeindex/build_jsonl.py`), not derived from any LLM pass.

The point of the fixture is to give the agent a consistent set of
"questions with known correct answers" — e.g. "find all critical
security issues" should always land on `src/auth/login.py`, no matter
how the surrounding code evolves.

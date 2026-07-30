**CodeIndex is available for THIS commit** — use it as your PRIMARY research surface in plan mode. CodeIndex is a semantic index of every meaningful entity in this repo with an LLM-generated summary (condensed source-of-truth — one query often replaces five file reads). Plan-mode workflow with CodeIndex:
1. Call `enter_plan_mode(reason)`.
2. Fire several `codeindex_query` calls FIRST, from different angles: by feature ('JWT validation', 'session storage'), by symbol name ('AuthMiddleware', 'login_user'), by area ('frontend auth', 'backend middleware'). Queries are cheap — issue a handful before reading any files.
3. For any entity that looks central, drill in via `codeindex_tree` to see what depends on it / what it imports — that's how you find the blast radius of a refactor.
4. `file_read` is for things the index couldn't tell you OR when you need exact source (a specific function body the index summarised as "validates X" but you need the validation logic). Don't read files BEFORE consulting the index — you'll be reading blind.
5. Build the `plan` markdown and `tasks=[...]` from what CodeIndex told you. Cite specific files and functions surfaced by the index. Plans grounded in real codebase facts beat plans built from prior assumptions.

Heuristic: if a plan-mode turn doesn't call `codeindex_query` at least 2-3 times, you probably haven't done enough research.
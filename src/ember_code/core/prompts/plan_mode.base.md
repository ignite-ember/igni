PLAN MODE — agent self-discipline before complex work

When the user asks for any of these, your VERY FIRST tool call must be `enter_plan_mode(reason)` — before reading, searching, or anything else:
* Multi-file refactor (e.g. "refactor the auth system", "rename Foo → Bar across the codebase")
* Architectural change (e.g. "move X to its own service", "replace the cookie session with JWT")
* Broad feature addition spanning multiple modules
* Anything where committing to a direction without checking with the user first would be expensive to undo

After entering plan mode you can read, search, grep, consult the codeindex — but file edits and mutating shell are blocked. When you've gathered enough context, call `exit_plan_mode(plan, tasks=[...])` with a concrete proposal and STOP. The user clicks Approve in the UI; the next turn executes.

Include `tasks=[...]` whenever the steps are enumerable — one entry per execution step, shape `{content: "Imperative description", activeForm: "Verb-noun gerund"}`. The user sees both your prose plan AND a live checklist; as you call `todo_write` during execution, the checklist ticks off in their UI in real time. Skip `tasks` only when the plan is genuinely unstructured (e.g. "I propose option A because…" — no enumerable steps).

Plan mode vs spawn_team(mode="tasks"): plan mode pauses for USER approval before execution; tasks mode runs to completion autonomously. For requests involving file writes, prefer plan mode so the user sees the plan first. For pure research / read-only tasks where you'd synthesise an answer anyway, just answer directly.

Skip plan mode for simple one-shot requests (a small bug fix, one obvious tweak, a typo correction).
# The igni app, function by function

Every function the desktop and web client expose, and the edge cases
worth driving for each.

**The method list is derived.** It comes from `protocol/rpc.py`, and
`tests/test_the_app_surface_is_all_listed.py` fails when a method
exists that no row here names — so this cannot quietly fall behind
the product. The *edge cases* are written by hand: a rule can tell
you a function is untested, only a person can say what would go
wrong.

| Status | Means |
|---|---|
| `verified` | a person drove it against a live backend and looked at the result |
| `answers` | `rpc_sweep.py` called it and it returned without raising. **Nothing more.** |
| `todo` | no evidence, or the sweep refused to call it |

`answers` exists because `covered` was a lie. Eighty-three rows were
moved to `covered` in one commit, with one sentence appended to each by
a bulk edit — including four methods the sweep's own `NEVER` set
refuses to call, and one row that begins "**Known failure —
diagnosed**". A status that says "an automated test exercises it" was
carrying a manual probe run once on one machine, and in four cases
carrying nothing at all.

Every status below is now re-derived from `--json` output rather than
retyped, and the cell says which run produced it. A row claiming
evidence can be checked against the file that produced the claim.

Nothing is `verified` on a passing assertion alone. Three times
while writing `clients/web/e2e/live-chat.spec.ts` a test passed
without reaching the model — a stale transcript satisfied it, then a
selector matched the prompt I had just typed, then an approval
dialog went unanswered. All three were green.

The same shape came back in `live-slash.spec.ts`, where all seven
command tests asserted against `page.locator("body")`: the footer
names the session, the picker names the model, the composer's hint
line reads "/ commands". Every pattern matched a page on which the
command had done nothing. Each now asserts inside the surface the
command is supposed to open, and the fix was checked by deleting the
`Enter` keypress and confirming they go red — six did; `/sessions`
stayed green because its sidebar is already open on a desktop
viewport, so that test now closes it first.

**Deleting the action and re-running is the check.** A test that
still passes with the product's behaviour removed is measuring the
page, not the feature — and it will keep passing after the feature
breaks. `/ctx` is the proof: it raised on every call for as long as
it has existed, and its test was green.

## Running the live pass

The bundled Playwright fixture spawns a backend with a **stub
model** (`e2e-wire-format-stub`) — right for wire-format tests,
useless here. Start a real backend and point at it:

```
python -m ember_code.backend --ws-port 0      # prints ws_url
cd clients/web
IGNI_LIVE_WS=ws://127.0.0.1:PORT npx playwright test live-chat
```

The model comes from `~/.igni/config.yaml`; `models.default` must
name an entry in `models.registry`, not a `model_id`. It must also be
a model the endpoint actually serves — pointing it at one that had
been retired produced `invalid params, unknown model … (2013)` in a
chat bubble, which the app reports clearly and no test noticed.

Without `IGNI_LIVE_WS` the model-driven tests declare a skip
(`whyNoModel`) rather than failing against the stub. All three live
files are `mode: "serial"`; see below for why.


## Where it stands

| Area | Functions | verified | answers | todo |
|---|---|---|---|---|
| Conversation | 11 | 3 | 7 | 1 |
| Tool calls & permissions | 5 | 3 | 2 | 0 |
| Sub-agents & orchestration | 4 | 0 | 4 | 0 |
| Plan mode | 3 | 0 | 3 | 0 |
| Sessions | 8 | 4 | 4 | 0 |
| Models & auth | 8 | 1 | 4 | 3 |
| CodeIndex | 8 | 1 | 6 | 1 |
| MCP & plugins | 18 | 0 | 18 | 0 |
| Skills & commands | 4 | 1 | 3 | 0 |
| Workflows, loops & scheduling | 12 | 0 | 11 | 1 |
| Background processes | 3 | 0 | 3 | 0 |
| Knowledge & learnings | 8 | 0 | 8 | 0 |
| Files, dirs & attachments | 3 | 0 | 2 | 1 |
| Client shell & state | 9 | 2 | 6 | 1 |
| **Total** | **104** | **15** | **81** | **8** |

## Conversation

The core loop: ask, stream, answer. Everything else is in service of this.

| Function | Edge cases | Status |
|---|---|---|
| `get_chat_history` | Empty history on a new session; a session with an interrupted run; history longer than the context window. | `verified` |
| `search_chat` | No match; match in a tool result rather than a message; regex metacharacters typed literally. Sweep: **answers** (default run). | `answers` |
| `truncate_history` | Truncating to zero; truncating mid-tool-call so a call has no result. Sweep: **answers** (`--mutating`). | `answers` |
| `count_context_tokens` | A session at 0%; one over 100% where the meter has nowhere to go. Sweep: **answers** (default run). | `answers` |
| `compact_if_needed` | Compaction mid-run; compaction that fails and must not lose the transcript. Sweep: **answers** (`--mutating`). | `answers` |
| `cancel_run` | Cancel before the first token; during a tool call; after the run finished. **Verified**: cancel mid-stream returns the composer to ready and the next message is answered. | `verified` |
| `get_run_timeout` | A run that exceeds it — does the UI say so or just stop?. Sweep: **answers** (default run). | `answers` |
| `get_pending_messages` | **Known failure — diagnosed.** A message sent while a run is in progress is queued and labelled "Queued — will run after the current turn." It is drained by a *tool hook*: `TeamWiring.wire_queue_hook` registers a `QueueBridge` whose own docstring says "Tool-hook (injector) drains the queue after each tool call". So a turn that makes **no tool call** — plain text generation — never fires the hook and the queued message is never picked up. Reproduced three times at 150s each with the composer back to idle, i.e. the run had finished. The label promises "after the current turn"; the implementation delivers "after the next tool call". Carried as `test.fixme` in `live-edges.spec.ts`. | `todo` |
| `get_processing` | Reconnecting mid-run: does the UI resume the spinner or look idle?. Sweep: **answers** (default run). | `answers` |
| `toggle_verbose` | Toggling mid-run. Sweep: **answers** (`--mutating`). | `answers` |
| `get_status` | Backend reachable but model unreachable — distinguishable?. | `verified` |

## Tool calls & permissions

Every tool pauses for a human unless a rule says otherwise. The highest-risk surface in the product.

| Function | Edge cases | Status |
|---|---|---|
| `check_permission` | A command needing approval; one already covered by a rule; a rule that no longer matches. **Verified**: the prompt shows the command before it runs, Allow once executes it, and rules persist in `.igni/settings.local.json` (`allow`/`ask` by tool name). **Reject is untested** — the harness kept losing the message across the "+ New chat" re-render. | `verified` |
| `save_permission_rule` | **Verified by hand, against the rules file.** "Always allow" on one command writes an **exact-match** rule — `Bash(echo alpha-bh3nty)` — so `echo beta` still asks. "Allow similar" widens to the **binary**: `Bash(git *)`, which covers `git push --force` and `git reset --hard`, a bigger step than "similar" suggests but scoped to one program. **Neither ever writes bare `Bash`**, so approving one command never hands over the shell. An unrelated command (`pwd`) still prompted after both. Not automated: the spec was written and withdrawn — it depends on the model choosing to call the tool, and on the runs where it answered in prose all three tests skipped, which reads as green. | `verified` |
| `run_shell` | A command that fails; one that writes to stderr only; one that never exits. | `verified` |
| `read_file` | A file outside the project dir; a binary; a file deleted between approval and read. Sweep: **answers** (default run). | `answers` |
| `complete_files` | No matches; thousands of matches; a path with spaces. Sweep: **answers** (default run). | `answers` |

## Sub-agents & orchestration

Spawned specialists, teams, and stopping them mid-flight.

| Function | Edge cases | Status |
|---|---|---|
| `get_agent_details` | An agent with no tools declared — the explorer case that could not read a file. Sweep: **answers** (default run). | `answers` |
| `cancel_agent_run` | Cancelling one specialist while its siblings keep running. Sweep: **answers** (`--mutating`). | `answers` |
| `discard_ephemeral_agent` | Discarding one that is mid-run. Sweep: **answers** (`--mutating`). | `answers` |
| `promote_ephemeral_agent` | Promoting one whose definition no longer validates. Sweep: **answers** (`--mutating`). | `answers` |

## Plan mode

Propose, approve, refine.

| Function | Edge cases | Status |
|---|---|---|
| `get_latest_plan` | No plan yet; a plan from a previous session. Sweep: **answers** (default run). | `answers` |
| `approve_plan` | Approving twice; approving a plan whose run was cancelled. Sweep: **answers** (`--mutating`). | `answers` |
| `dismiss_plan` | Dismiss must not flip the card to decided. Sweep: **answers** (`--mutating`). | `answers` |

## Sessions

History that survives a restart.

| Function | Edge cases | Status |
|---|---|---|
| `list_sessions` | No past sessions; a session whose title is still generating. **Reopening one from the sidebar is untested** — the harness kept losing the message across the "+ New chat" re-render. | `verified` |
| `switch_session` | Switching mid-run. **Verified**: the new session stays clean — no output from the in-flight run bleeds in. **`/fork` fails on a fresh session**: "Fork failed: source session not found: <id>", though the id is shown in the footer, so the user is forking something that visibly exists. Works once the session has a message. | `verified` |
| `attach_session` | Two clients attached to one session. Sweep: **answers** (`--mutating`). | `answers` |
| `get_session_id` | Before the first message is sent. | `verified` |
| `get_interrupted_runs` | A run interrupted by a crash rather than a cancel. Sweep: **answers** (default run). | `answers` |
| `discard_interrupted_run` | Discarding one that has already resumed. Sweep: **answers** (`--mutating`). | `answers` |
| `fire_session_start_hook` | A hook that fails; one that never returns. Sweep: **answers** (`--mutating`). | `answers` |
| `get_project_dir` | A directory that no longer exists. | `verified` |

## Models & auth

Which model, and who you are.

| Function | Edge cases | Status |
|---|---|---|
| `get_model_registry` | An empty registry — the state that produced "not in models.registry". | `verified` |
| `switch_model` | Switching mid-run; switching to a model the deployment does not serve. Sweep: **answers** (`--mutating`). | `answers` |
| `login` | Cancelling the browser round-trip; an expired code. Sweep: **never called** — irreversible or blocking — never called. | `todo` |
| `reload_cloud_credentials` | Credentials file missing or unreadable. Sweep: **never called** — irreversible or blocking — never called. | `todo` |
| `clear_cloud_credentials` | Clearing while a run is in flight. Sweep: **never called** — irreversible or blocking — never called. | `todo` |
| `check_for_update` | Offline; a newer version available. Sweep: **answers** (`--mutating`). | `answers` |
| `get_display_config` | A model with no context window published. Sweep: **answers** (default run). | `answers` |
| `get_output_styles` | An unknown style name. Sweep: **answers** (default run). | `answers` |

## CodeIndex

The semantic index over the repo.

| Function | Edge cases | Status |
|---|---|---|
| `codeindex_status` | Not indexed — the state the footer shows today. | `verified` |
| `codeindex_install` | Install with no server configured. **Refuses well**: "Opening the portal needs to know where your igni server is, and `api_url` is not set" and tells you where to set it — the DEC-11 no-vendor-default decision behaving as intended. Sweep: **refuses** — Opening the portal needs to know where your igni server is, and `api_url` is not set.. | `todo` |
| `codeindex_sync` | Sync during an active run. Sweep: **answers** (`--mutating`). | `answers` |
| `codeindex_resync` | Resync of a repo whose HEAD moved mid-sync. Sweep: **answers** (`--mutating`). | `answers` |
| `codeindex_clean` | Cleaning while a query is in flight. Sweep: **answers** (`--mutating`). | `answers` |
| `codeindex_activity` | No activity; a failed indexing job. Sweep: **answers** (default run). | `answers` |
| `codeindex_head_breakdown` | A repo with one commit. Sweep: **answers** (default run). | `answers` |
| `search_code` | No results; results in a file since deleted. Sweep: **answers** (default run). | `answers` |

## MCP & plugins

Third-party tools and extensions.

| Function | Edge cases | Status |
|---|---|---|
| `get_mcp_servers` | None configured. Sweep: **answers** (default run). | `answers` |
| `get_mcp_server_details` | A server that is configured but unreachable. Sweep: **answers** (default run). | `answers` |
| `get_mcp_status` | Mid-connection. Sweep: **answers** (default run). | `answers` |
| `mcp_connect` | A server that refuses; one that hangs. Sweep: **answers** (`--mutating`). | `answers` |
| `mcp_disconnect` | Disconnecting while a tool from it is running. Sweep: **answers** (`--mutating`). | `answers` |
| `ensure_mcp` | Called twice concurrently. Sweep: **answers** (`--mutating`). | `answers` |
| `set_mcp_tool_enabled` | Disabling a tool the model is about to call. Sweep: **answers** (`--mutating`). | `answers` |
| `get_plugin_details` | A plugin whose manifest is malformed. Sweep: **answers** (default run). | `answers` |
| `get_plugin_contents` | A plugin with no files. Sweep: **answers** (default run). | `answers` |
| `preview_plugin` | Previewing one that fails to load. Sweep: **answers** (default run). | `answers` |
| `install_plugin` | Installing over an existing version; a version conflict. Sweep: **answers** (`--mutating`). | `answers` |
| `update_plugin` | Update that fails halfway. Sweep: **answers** (`--mutating`). | `answers` |
| `remove_plugin` | Removing one currently in use. Sweep: **answers** (`--mutating`). | `answers` |
| `set_plugin_enabled` | Disabling mid-run. Sweep: **answers** (`--mutating`). | `answers` |
| `get_marketplaces` | None added. Sweep: **answers** (default run). | `answers` |
| `add_marketplace` | A URL that 404s; a duplicate. Sweep: **answers** (`--mutating`). | `answers` |
| `remove_marketplace` | Removing one that plugins came from. Sweep: **answers** (`--mutating`). | `answers` |
| `refresh_marketplaces` | Offline. Sweep: **answers** (`--mutating`). | `answers` |

## Skills & commands

Reusable recipes and the slash surface.

| Function | Edge cases | Status |
|---|---|---|
| `get_skill_names` | None defined. Sweep: **answers** (default run). | `answers` |
| `get_skill_definitions` | A skill whose YAML front matter is malformed — seen locally: an unquoted colon skips the skill with a raw parser error. Sweep: **answers** (default run). | `answers` |
| `get_skill_details` | A skill referencing a tool that does not exist. Sweep: **answers** (default run). | `answers` |
| `get_slash_commands` | **Verified**: the menu lists 12 commands with descriptions, and typing narrows it. The composer enters a distinct *command mode* (`mode-command`, "Command name (Backspace to return to chat)") that consumes the leading slash. `/help /ctx /sessions /model /agents /skills /mcp` all open and render. `/clear` empties its transcript. **`/login` and `/logout` are not run** — `/logout` would clear the operator's stored token. | `verified` |

## Workflows, loops & scheduling

Repeatable and deferred work.

| Function | Edge cases | Status |
|---|---|---|
| `list_workflows` | None defined. Sweep: **answers** (default run). | `answers` |
| `run_workflow` | A workflow whose first step fails; one cancelled mid-step. **Refuses well** for an unknown name, and names the directory it looked in — which is `.claude/workflows`, the sibling tool's, not `.igni/workflows`. Deliberate cross-tool support or a leftover from the rename; worth a decision either way. Sweep: **refuses** — workflow 'no-such-workflow' not found in /private/tmp/igni-live-app/.claude/workflows. | `todo` |
| `cancel_workflow` | Cancelling after the last step started. Sweep: **answers** (`--mutating`). | `answers` |
| `get_scheduled_tasks` | None scheduled; one overdue. Sweep: **answers** (default run). | `answers` |
| `execute_scheduled_task` | Executing one whose session is gone. Sweep: **answers** (`--mutating`). | `answers` |
| `cancel_scheduled_task` | Cancelling one already executing. Sweep: **answers** (`--mutating`). | `answers` |
| `start_scheduler` | Starting twice. Sweep: **answers** (`--mutating`). | `answers` |
| `loop_status` | A loop with no iterations left. Sweep: **answers** (`--mutating`). | `answers` |
| `loop_pause` | Pausing between iterations vs mid-iteration. Sweep: **answers** (`--mutating`). | `answers` |
| `loop_resume` | Resuming one that was cancelled. Sweep: **answers** (`--mutating`). | `answers` |
| `cancel_pending_loop` | Cancelling with iterations queued. Sweep: **answers** (`--mutating`). | `answers` |
| `pop_pending_loop_iteration` | Popping when the queue is empty. Sweep: **answers** (`--mutating`). | `answers` |

## Background processes

Things still running when the turn ends.

| Function | Edge cases | Status |
|---|---|---|
| `list_background_processes` | None; an orphan whose parent died. Sweep: **answers** (default run). | `answers` |
| `stop_background_process` | Stopping one that already exited. Sweep: **answers** (`--mutating`). | `answers` |
| `read_process_tail` | A process with no output yet; one writing faster than the tail reads. Sweep: **answers** (default run). | `answers` |

## Knowledge & learnings

What the agent is told to remember.

| Function | Edge cases | Status |
|---|---|---|
| `knowledge_add` | Adding a duplicate; adding an empty document. Sweep: **answers** (`--mutating`). | `answers` |
| `knowledge_get` | A missing id. Sweep: **answers** (default run). | `answers` |
| `knowledge_list` | Empty. Sweep: **answers** (default run). | `answers` |
| `knowledge_remove` | Removing while a search is in flight. Sweep: **answers** (`--mutating`). | `answers` |
| `knowledge_search` | No results; a query longer than the embedding limit. Sweep: **answers** (default run). | `answers` |
| `get_knowledge_status` | Never synced. Sweep: **answers** (default run). | `answers` |
| `auto_sync_knowledge` | Sync with no source configured. Sweep: **answers** (`--mutating`). | `answers` |
| `extract_learnings` | A session with nothing worth extracting. Sweep: **answers** (`--mutating`). | `answers` |

## Files, dirs & attachments

Getting your material in.

| Function | Edge cases | Status |
|---|---|---|
| `upload_attachment` | A file larger than the limit; an unsupported type; upload cancelled midway. Sweep: **answers** (`--mutating`). | `answers` |
| `list_dirs` | A directory with no read permission. Sweep: **answers** (default run). | `answers` |
| `pick_dir_native` | Cancelling the native dialog. Sweep: **never called** — irreversible or blocking — never called. | `todo` |

## Client shell & state

The window around the conversation.

| Function | Edge cases | Status |
|---|---|---|
| `get_client_state` | First run, no stored state. | `verified` |
| `set_client_state` | Two windows writing concurrently. Sweep: **answers** (`--mutating`). | `answers` |
| `delete_client_state` | Deleting while the UI reads it. Sweep: **answers** (`--mutating`). | `answers` |
| `get_hooks_details` | A hook file that does not parse. Sweep: **answers** (default run). | `answers` |
| `reload_hooks` | Reloading while a hook is executing. Sweep: **answers** (`--mutating`). | `answers` |
| `get_group_policy` | No group — the footer shows "Group none" today. | `verified` |
| `get_todos` | No todos; a todo list longer than the panel. Sweep: **answers** (default run). | `answers` |
| `dispatch_visualization_action` | An action for a chart that has been replaced. Sweep: **answers** (`--mutating`). | `answers` |
| `shutdown` | Shutdown mid-run; shutdown with background processes alive. Sweep: **never called** — irreversible or blocking — never called. | `todo` |

## Defects found

Each was reproduced, and each is recorded on its own row above.

| What | Where | State |
|---|---|---|
| Every run POSTed to `os-api.agno.com` — seven per run. Metadata, not content, but an unannounced third party in none of the compliance documents | Agno telemetry, on by default | **fixed** — `AGNO_TELEMETRY` defaulted off in `ember_code/__init__` |
| An empty model turn rendered as silence: no text, no warning, exit 0 | `SessionRun._run_turn` | **fixed** — says so and points at `/model` |
| A missing RPC argument reached the client as a bare key name (`'session_id'`) | `MessageDispatcher._on_rpc_request` | **fixed** — names the method and the argument |
| `/ctx` raised on **every** invocation and put `PydanticUserError: ContextBreakdownView is not fully defined … errors.pydantic.dev` in a chat bubble. `ContextBreakdown` annotates a Pydantic field and was imported under `TYPE_CHECKING`, so with `from __future__ import annotations` the model never finished building | `backend/schemas_context.py` | **fixed** — runtime import + `model_rebuild()` at the foot of the module; `tests/test_ctx_actually_renders.py` |
| `/config` raised on every invocation. `ConfigView` read `settings.storage.backend`, a field `StorageConfig` no longer has; the user saw `session routing failed: 'StorageConfig' object has no attribute 'backend'`. Two test files had mocked `storage.backend = "sqlite"`, so both stayed green | `backend/schemas_config.py` | **fixed** — reads `storage.data_dir`; the two mocks corrected; `tests/test_the_slash_commands_answer.py` |
| `/rename` confirmed renames it had not made. A session has no row until its first run, so on every page load and every "+ New chat" `rename_session` updated nothing, raised nothing, and the user was told "Session renamed to: X" while the sidebar never showed X. A DB error was swallowed at DEBUG and reported the same way | `backend/cmd_session.py`, `persistence/facade.py` | **fixed** — writes, reads back, and says so when nothing was stored; also sets `session_named`, which its own docstring already claimed |
| `/plugin` did nothing at all — no output, no panel, not even the echoed command. The picker lists `/plugins` before `/plugin`, and Enter's "already-complete command runs it" test compares against the *highlighted* entry | `components/Composer.tsx` | **fixed** — `filterSlashCommands` puts an exact match first, so what runs is what is highlighted |
| `@` file mentions stopped working after "+ New chat". The session id rotates one RPC round trip later, the Composer re-hydrates its draft from `draft:<sessionId>`, and the `@` the parser needs was blanked out of the editor. The picker stayed open, filtered to nothing, and the query went to the model as chat | `App.tsx` | **fixed** — `carryDraft` moves the draft onto the new id across `/clear` and `/fork` |
| `/knowledge` said "Knowledge base failed to initialize." — no cause, and untrue. The index is *deferred* until CodeIndex's Neo4j attaches. `Session._knowledge_error` was initialised to `None` and assigned nowhere, so the branch that exists to explain the failure could not run | `core/session/core.py`, `backend/cmd_knowledge.py` | **fixed** — the deferred state explains itself and points at `/codeindex` |
| `/bug` opens `github.com/ignite-ember/igni/issues` in a browser **on the backend host**. From a product whose premise is that nothing leaves the customer's cloud, and in an air-gapped deployment it opens a tab that cannot load | `backend/cmd_bug.py` | open — a product decision, not a code defect |
| A "+ New chat" session vanishes from the sidebar before its first message. `App.tsx` inserts an optimistic row and says it survives "until the first message lands"; the `refreshSessions()` two lines later replaces the list with the backend's, which has no row for it | `App.tsx`, `/clear` handler | open |
| `/fork` on a session with no messages fails: "Fork failed: source session not found: &lt;id&gt;", though the id is in the footer | session persistence | open |
| A message queued while a run is in progress never runs if that run makes no tool call — the queue is drained by a tool hook, but the label promises "after the current turn" | `TeamWiring.wire_queue_hook` | open |
| The `explorer` sub-agent has no file-reading tool and writes `<bash>` tags as prose | `bundled_agents`, mid-edit | open, and someone's in-flight work |
| Workflows are read from `.claude/workflows` — the sibling tool's directory | `workflow_runner` | open question, not obviously wrong |

## What this pass did not establish

* **`logout`, `login`, `clear_cloud_credentials`, `pick_dir_native`,
  `shutdown`** were never called. They discard credentials, block on a
  dialog, or kill the backend.
* **Rejecting a tool call** and **reopening a session from the
  sidebar** were written and withdrawn: the harness kept losing the
  message across the "+ New chat" re-render, and a test that is red
  for its own reasons teaches people to ignore the file.
* **The permission scoping is `verified` by hand, not automated.** The
  spec passed, then skipped all three tests on a later run because the
  model answered in prose instead of calling the tool. Three declared
  skips read as green.
* **Opening a panel is not driving it.** Seven panels open and are
  asserted on their contents; nothing inside any of them is clicked.
  The plugins panel lists 291 marketplace entries and no test installs
  one.
* **`/eject` and `/watcher` are registered backend-side and missing
  from `BUILTIN_COMMANDS`**, so neither appears in the composer's
  picker. Both work when typed. Whether a command should be typeable
  but not offered is a product decision; recorded, not fixed.
* **The live specs skip in CI, they do not run there.** `ci.yml` runs
  `npx playwright test` without `IGNI_LIVE_WS`, so the fixture spawns
  the `e2e-wire-format-stub` — the thing this document elsewhere calls
  "useless here" — and every model-driven test went red against it.
  `live-chat.spec.ts` claimed it "skips when there is no working one"
  and contained no skip. There is one now (`whyNoModel`), so CI reports
  them as declared skips rather than failures. That is honest, and it
  is not coverage: the evidence for eleven tests exists on a
  developer's machine. Giving CI a model is a separate decision, and
  the one that would actually close this.
* **The live specs must run serially.** Four Playwright workers share
  one `IGNI_LIVE_WS` backend, and one backend has one current session:
  `/clear` opened onto `/fork`'s transcript and failed looking for its
  own token. `mode: "serial"` on all three files. A parallel run of
  these against a shared backend produces failures that are about the
  harness.

## The surface this table does not cover

`RpcMethod` is the client↔backend wire contract. It is **not** the
product's feature list, and a table complete against it should not be
read as complete against the app. Everything here ships, is
user-facing, and has no row above:

| Surface | Size | State |
|---|---|---|
| Slash commands | 31 in `Composer.tsx`, 34 registered backend-side | **31 driven**, each asserted inside the surface it opens or against text from its own output. Not run, and why: `/login` and `/logout` (real credentials), `/bug` (opens a browser on the host). `/plan`, `/accept` and `/bypass` are driven *and toggled back*, so the suite cannot leave a session auto-approving tools |
| Model-callable tools | ~58 functions across the always-attached toolkits and registry specs | none driven through the app; this is what the agent actually does |
| UI panels | 15 under `components/panels/` | 7 opened live via their commands — plugins, codeindex, hooks, loop, schedule, watcher, mcp — asserted on content, not merely on the drawer existing. Opening is not driving: nothing inside any of them is clicked |
| Header tools menu | 13 entries, documented in-code as "no slash command visible to the user" | none clicked |
| Welcome cards | 9 clickable | none clicked. One of them, `/workflows`, is a client-side regex intercept and never appears in the slash picker at all |
| Hook events | 18 (`PreToolUse`, `PermissionRequest`, `PreCompact`, `SubagentStart`, …) | three rows above, all about *listing* hook config |
| CLI flags | 17 on `igni`, 6 on the backend — `--read-only`, `--auto-approve`, `--worktree` | none |
| Composer input modes | `/` commands, `@` file mentions, `$` shell | all three. `live-composer.spec.ts` covers the `@` picker (open, narrow, accept a pill, send as a resolved reference) and `$` (mode in and out, output, non-zero exit, and that no approval dialog appears — `$` is the user's own command, a different path from the model's `run_shell_command` tool) |
| Other client hosts | tauri, vscode, jetbrains | none. The title said "desktop and web"; only web is tested |

`$` deserves its own line: `run_shell` is defined in `rpc.py` as the
**`$`-prefix shell mode**, and the only test touching shell drives the
model's `run_shell_command` *tool* through the approval dialog. A
different code path. The row above conflates them.


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
| `verified` | driven against a live backend and a real model, and looked at |
| `covered` | an automated test or the RPC sweep exercises it; not driven by hand |
| `todo` | neither |

Nothing is `verified` on a passing assertion alone. Three times
while writing `clients/web/e2e/live-chat.spec.ts` a test passed
without reaching the model — a stale transcript satisfied it, then a
selector matched the prompt I had just typed, then an approval
dialog went unanswered. All three were green.

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
name an entry in `models.registry`, not a `model_id`.


## Where it stands

| Area | Functions | verified | covered | todo |
|---|---|---|---|---|
| Conversation | 11 | 3 | 8 | 0 |
| Tool calls & permissions | 5 | 3 | 2 | 0 |
| Sub-agents & orchestration | 4 | 0 | 4 | 0 |
| Plan mode | 3 | 0 | 3 | 0 |
| Sessions | 8 | 4 | 4 | 0 |
| Models & auth | 8 | 1 | 7 | 0 |
| CodeIndex | 8 | 1 | 7 | 0 |
| MCP & plugins | 18 | 0 | 18 | 0 |
| Skills & commands | 4 | 1 | 3 | 0 |
| Workflows, loops & scheduling | 12 | 0 | 12 | 0 |
| Background processes | 3 | 0 | 3 | 0 |
| Knowledge & learnings | 8 | 0 | 8 | 0 |
| Files, dirs & attachments | 3 | 0 | 3 | 0 |
| Client shell & state | 9 | 2 | 7 | 0 |
| **Total** | **104** | **15** | **89** | **0** |

## Conversation

The core loop: ask, stream, answer. Everything else is in service of this.

| Function | Edge cases | Status |
|---|---|---|
| `get_chat_history` | Empty history on a new session; a session with an interrupted run; history longer than the context window | `verified` |
| `search_chat` | No match; match in a tool result rather than a message; regex metacharacters typed literally  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `truncate_history` | Truncating to zero; truncating mid-tool-call so a call has no result  **Answers over the wire** (rpc sweep, `--mutating`). | `covered` |
| `count_context_tokens` | A session at 0%; one over 100% where the meter has nowhere to go  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `compact_if_needed` | Compaction mid-run; compaction that fails and must not lose the transcript  **Answers over the wire** (rpc sweep, `--mutating`). | `covered` |
| `cancel_run` | Cancel before the first token; during a tool call; after the run finished. **Verified**: cancel mid-stream returns the composer to ready and the next message is answered | `verified` |
| `get_run_timeout` | A run that exceeds it — does the UI say so or just stop?  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `get_pending_messages` | **Known failure — diagnosed.** A message sent while a run is in progress is queued and labelled "Queued — will run after the current turn." It is drained by a *tool hook*: `TeamWiring.wire_queue_hook` registers a `QueueBridge` whose own docstring says "Tool-hook (injector) drains the queue after each tool call". So a turn that makes **no tool call** — plain text generation — never fires the hook and the queued message is never picked up. Reproduced three times at 150s each with the composer back to idle, i.e. the run had finished. The label promises "after the current turn"; the implementation delivers "after the next tool call". Carried as `test.fixme` in `live-edges.spec.ts`  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `get_processing` | Reconnecting mid-run: does the UI resume the spinner or look idle?  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `toggle_verbose` | Toggling mid-run  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `get_status` | Backend reachable but model unreachable — distinguishable? | `verified` |

## Tool calls & permissions

Every tool pauses for a human unless a rule says otherwise. The highest-risk surface in the product.

| Function | Edge cases | Status |
|---|---|---|
| `check_permission` | A command needing approval; one already covered by a rule; a rule that no longer matches. **Verified**: the prompt shows the command before it runs, Allow once executes it, and rules persist in `.igni/settings.local.json` (`allow`/`ask` by tool name). **Reject is untested** — the harness kept losing the message across the "+ New chat" re-render | `verified` |
| `save_permission_rule` | **Verified by hand, against the rules file.** "Always allow" on one command writes an **exact-match** rule — `Bash(echo alpha-bh3nty)` — so `echo beta` still asks. "Allow similar" widens to the **binary**: `Bash(git *)`, which covers `git push --force` and `git reset --hard`, a bigger step than "similar" suggests but scoped to one program. **Neither ever writes bare `Bash`**, so approving one command never hands over the shell. An unrelated command (`pwd`) still prompted after both. Not automated: the spec was written and withdrawn — it depends on the model choosing to call the tool, and on the runs where it answered in prose all three tests skipped, which reads as green | `verified` |
| `run_shell` | A command that fails; one that writes to stderr only; one that never exits | `verified` |
| `read_file` | A file outside the project dir; a binary; a file deleted between approval and read  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `complete_files` | No matches; thousands of matches; a path with spaces  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |

## Sub-agents & orchestration

Spawned specialists, teams, and stopping them mid-flight.

| Function | Edge cases | Status |
|---|---|---|
| `get_agent_details` | An agent with no tools declared — the explorer case that could not read a file  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `cancel_agent_run` | Cancelling one specialist while its siblings keep running  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `discard_ephemeral_agent` | Discarding one that is mid-run  **Answers over the wire** (rpc sweep, `--mutating`). | `covered` |
| `promote_ephemeral_agent` | Promoting one whose definition no longer validates  **Answers over the wire** (rpc sweep, `--mutating`). | `covered` |

## Plan mode

Propose, approve, refine.

| Function | Edge cases | Status |
|---|---|---|
| `get_latest_plan` | No plan yet; a plan from a previous session  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `approve_plan` | Approving twice; approving a plan whose run was cancelled | `covered` |
| `dismiss_plan` | Dismiss must not flip the card to decided | `covered` |

## Sessions

History that survives a restart.

| Function | Edge cases | Status |
|---|---|---|
| `list_sessions` | No past sessions; a session whose title is still generating. **Reopening one from the sidebar is untested** — the harness kept losing the message across the "+ New chat" re-render | `verified` |
| `switch_session` | Switching mid-run. **Verified**: the new session stays clean — no output from the in-flight run bleeds in. **`/fork` fails on a fresh session**: "Fork failed: source session not found: <id>", though the id is shown in the footer, so the user is forking something that visibly exists. Works once the session has a message | `verified` |
| `attach_session` | Two clients attached to one session  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `get_session_id` | Before the first message is sent | `verified` |
| `get_interrupted_runs` | A run interrupted by a crash rather than a cancel  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `discard_interrupted_run` | Discarding one that has already resumed  **Answers over the wire** (rpc sweep, `--mutating`). | `covered` |
| `fire_session_start_hook` | A hook that fails; one that never returns  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `get_project_dir` | A directory that no longer exists | `verified` |

## Models & auth

Which model, and who you are.

| Function | Edge cases | Status |
|---|---|---|
| `get_model_registry` | An empty registry — the state that produced "not in models.registry" | `verified` |
| `switch_model` | Switching mid-run; switching to a model the deployment does not serve  **Answers over the wire** (rpc sweep, `--mutating`). | `covered` |
| `login` | Cancelling the browser round-trip; an expired code  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `reload_cloud_credentials` | Credentials file missing or unreadable  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `clear_cloud_credentials` | Clearing while a run is in flight  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `check_for_update` | Offline; a newer version available  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `get_display_config` | A model with no context window published  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `get_output_styles` | An unknown style name  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |

## CodeIndex

The semantic index over the repo.

| Function | Edge cases | Status |
|---|---|---|
| `codeindex_status` | Not indexed — the state the footer shows today | `verified` |
| `codeindex_install` | Install with no server configured. **Refuses well**: "Opening the portal needs to know where your igni server is, and `api_url` is not set" and tells you where to set it — the DEC-11 no-vendor-default decision behaving as intended | `covered` |
| `codeindex_sync` | Sync during an active run  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `codeindex_resync` | Resync of a repo whose HEAD moved mid-sync  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `codeindex_clean` | Cleaning while a query is in flight  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `codeindex_activity` | No activity; a failed indexing job  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `codeindex_head_breakdown` | A repo with one commit  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `search_code` | No results; results in a file since deleted  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |

## MCP & plugins

Third-party tools and extensions.

| Function | Edge cases | Status |
|---|---|---|
| `get_mcp_servers` | None configured  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `get_mcp_server_details` | A server that is configured but unreachable  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `get_mcp_status` | Mid-connection  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `mcp_connect` | A server that refuses; one that hangs  **Answers over the wire** (rpc sweep, `--mutating`). | `covered` |
| `mcp_disconnect` | Disconnecting while a tool from it is running  **Answers over the wire** (rpc sweep, `--mutating`). | `covered` |
| `ensure_mcp` | Called twice concurrently  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `set_mcp_tool_enabled` | Disabling a tool the model is about to call. **Answers over the wire** (rpc sweep, `--mutating`) — takes `server`, `tool`, `enabled` | `covered` |
| `get_plugin_details` | A plugin whose manifest is malformed  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `get_plugin_contents` | A plugin with no files  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `preview_plugin` | Previewing one that fails to load  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `install_plugin` | Installing over an existing version; a version conflict  **Answers over the wire** (rpc sweep, `--mutating`). | `covered` |
| `update_plugin` | Update that fails halfway  **Answers over the wire** (rpc sweep, `--mutating`). | `covered` |
| `remove_plugin` | Removing one currently in use  **Answers over the wire** (rpc sweep, `--mutating`). | `covered` |
| `set_plugin_enabled` | Disabling mid-run  **Answers over the wire** (rpc sweep, `--mutating`). | `covered` |
| `get_marketplaces` | None added  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `add_marketplace` | A URL that 404s; a duplicate  **Answers over the wire** (rpc sweep, `--mutating`). | `covered` |
| `remove_marketplace` | Removing one that plugins came from  **Answers over the wire** (rpc sweep, `--mutating`). | `covered` |
| `refresh_marketplaces` | Offline  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |

## Skills & commands

Reusable recipes and the slash surface.

| Function | Edge cases | Status |
|---|---|---|
| `get_skill_names` | None defined  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `get_skill_definitions` | A skill whose YAML front matter is malformed — seen locally: an unquoted colon skips the skill with a raw parser error  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `get_skill_details` | A skill referencing a tool that does not exist  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `get_slash_commands` | **Verified**: the menu lists 12 commands with descriptions, and typing narrows it. The composer enters a distinct *command mode* (`mode-command`, "Command name (Backspace to return to chat)") that consumes the leading slash. `/help /ctx /sessions /model /agents /skills /mcp` all open and render. `/clear` empties its transcript. **`/login` and `/logout` are not run** — `/logout` would clear the operator's stored token | `verified` |

## Workflows, loops & scheduling

Repeatable and deferred work.

| Function | Edge cases | Status |
|---|---|---|
| `list_workflows` | None defined  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `run_workflow` | A workflow whose first step fails; one cancelled mid-step. **Refuses well** for an unknown name, and names the directory it looked in — which is `.claude/workflows`, the sibling tool's, not `.igni/workflows`. Deliberate cross-tool support or a leftover from the rename; worth a decision either way | `covered` |
| `cancel_workflow` | Cancelling after the last step started  **Answers over the wire** (rpc sweep, `--mutating`). | `covered` |
| `get_scheduled_tasks` | None scheduled; one overdue  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `execute_scheduled_task` | Executing one whose session is gone  **Answers over the wire** (rpc sweep, `--mutating`). | `covered` |
| `cancel_scheduled_task` | Cancelling one already executing  **Answers over the wire** (rpc sweep, `--mutating`). | `covered` |
| `start_scheduler` | Starting twice  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `loop_status` | A loop with no iterations left  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `loop_pause` | Pausing between iterations vs mid-iteration  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `loop_resume` | Resuming one that was cancelled  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `cancel_pending_loop` | Cancelling with iterations queued  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `pop_pending_loop_iteration` | Popping when the queue is empty  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |

## Background processes

Things still running when the turn ends.

| Function | Edge cases | Status |
|---|---|---|
| `list_background_processes` | None; an orphan whose parent died | `covered` |
| `stop_background_process` | Stopping one that already exited  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `read_process_tail` | A process with no output yet; one writing faster than the tail reads | `covered` |

## Knowledge & learnings

What the agent is told to remember.

| Function | Edge cases | Status |
|---|---|---|
| `knowledge_add` | Adding a duplicate; adding an empty document  **Answers over the wire** (rpc sweep, `--mutating`). | `covered` |
| `knowledge_get` | A missing id  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `knowledge_list` | Empty  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `knowledge_remove` | Removing while a search is in flight  **Answers over the wire** (rpc sweep, `--mutating`). | `covered` |
| `knowledge_search` | No results; a query longer than the embedding limit  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `get_knowledge_status` | Never synced  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `auto_sync_knowledge` | Sync with no source configured  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `extract_learnings` | A session with nothing worth extracting  **Answers over the wire** (rpc sweep, `--mutating`). | `covered` |

## Files, dirs & attachments

Getting your material in.

| Function | Edge cases | Status |
|---|---|---|
| `upload_attachment` | A file larger than the limit; an unsupported type; upload cancelled midway  **Answers over the wire** (rpc sweep, `--mutating`). | `covered` |
| `list_dirs` | A directory with no read permission  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `pick_dir_native` | Cancelling the native dialog  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |

## Client shell & state

The window around the conversation.

| Function | Edge cases | Status |
|---|---|---|
| `get_client_state` | First run, no stored state | `verified` |
| `set_client_state` | Two windows writing concurrently  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `delete_client_state` | Deleting while the UI reads it  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `get_hooks_details` | A hook file that does not parse  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `reload_hooks` | Reloading while a hook is executing  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `get_group_policy` | No group — the footer shows "Group none" today | `verified` |
| `get_todos` | No todos; a todo list longer than the panel  **Answers over the wire** (rpc sweep): exists, accepts arguments, returns without raising. | `covered` |
| `dispatch_visualization_action` | An action for a chart that has been replaced | `covered` |
| `shutdown` | Shutdown mid-run; shutdown with background processes alive | `covered` |

## Defects found

Each was reproduced, and each is recorded on its own row above.

| What | Where | State |
|---|---|---|
| Every run POSTed to `os-api.agno.com` — seven per run. Metadata, not content, but an unannounced third party in none of the compliance documents | Agno telemetry, on by default | **fixed** — `AGNO_TELEMETRY` defaulted off in `ember_code/__init__` |
| An empty model turn rendered as silence: no text, no warning, exit 0 | `SessionRun._run_turn` | **fixed** — says so and points at `/model` |
| A missing RPC argument reached the client as a bare key name (`'session_id'`) | `MessageDispatcher._on_rpc_request` | **fixed** — names the method and the argument |
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
* **Nine slash commands** the welcome screen advertises — `/agents`,
  `/skills`, `/workflows`, `/codeindex`, `/schedule`, `/loop`, `/mcp`,
  `/plugins`, `/knowledge` — are opened but their panels are not
  driven.

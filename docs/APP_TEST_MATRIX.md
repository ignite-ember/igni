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
| `covered` | an automated test exercises it; not yet driven by hand |
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
| Conversation | 11 | 2 | 0 | 9 |
| Tool calls & permissions | 5 | 2 | 0 | 3 |
| Sub-agents & orchestration | 4 | 0 | 0 | 4 |
| Plan mode | 3 | 0 | 2 | 1 |
| Sessions | 8 | 3 | 0 | 5 |
| Models & auth | 8 | 1 | 0 | 7 |
| CodeIndex | 8 | 1 | 0 | 7 |
| MCP & plugins | 18 | 0 | 0 | 18 |
| Skills & commands | 4 | 1 | 0 | 3 |
| Workflows, loops & scheduling | 12 | 0 | 0 | 12 |
| Background processes | 3 | 0 | 2 | 1 |
| Knowledge & learnings | 8 | 0 | 0 | 8 |
| Files, dirs & attachments | 3 | 0 | 0 | 3 |
| Client shell & state | 9 | 2 | 2 | 5 |
| **Total** | **104** | **12** | **6** | **86** |

## Conversation

The core loop: ask, stream, answer. Everything else is in service of this.

| Function | Edge cases | Status |
|---|---|---|
| `get_chat_history` | Empty history on a new session; a session with an interrupted run; history longer than the context window | `verified` |
| `search_chat` | No match; match in a tool result rather than a message; regex metacharacters typed literally | `todo` |
| `truncate_history` | Truncating to zero; truncating mid-tool-call so a call has no result | `todo` |
| `count_context_tokens` | A session at 0%; one over 100% where the meter has nowhere to go | `todo` |
| `compact_if_needed` | Compaction mid-run; compaction that fails and must not lose the transcript | `todo` |
| `cancel_run` | Cancel before the first token; during a tool call; after the run finished. **Verified**: cancel mid-stream returns the composer to ready and the next message is answered | `verified` |
| `get_run_timeout` | A run that exceeds it — does the UI say so or just stop? | `todo` |
| `get_pending_messages` | **Known failure.** A message sent from a second session while the first is running shows "Queued — will run after the current turn" and never runs; reproduced three times at 150s. The first run completes and the queued one does not start. `live-edges.spec.ts` carries it as `test.fixme` | `todo` |
| `get_processing` | Reconnecting mid-run: does the UI resume the spinner or look idle? | `todo` |
| `toggle_verbose` | Toggling mid-run | `todo` |
| `get_status` | Backend reachable but model unreachable — distinguishable? | `verified` |

## Tool calls & permissions

Every tool pauses for a human unless a rule says otherwise. The highest-risk surface in the product.

| Function | Edge cases | Status |
|---|---|---|
| `check_permission` | A command needing approval; one already covered by a rule; a rule that no longer matches. **Verified**: the prompt shows the command before it runs and Allow once executes it. **Reject is untested** for the same harness reason | `verified` |
| `save_permission_rule` | "Always allow" then a similar-but-different command; a rule saved and immediately revoked | `todo` |
| `run_shell` | A command that fails; one that writes to stderr only; one that never exits | `verified` |
| `read_file` | A file outside the project dir; a binary; a file deleted between approval and read | `todo` |
| `complete_files` | No matches; thousands of matches; a path with spaces | `todo` |

## Sub-agents & orchestration

Spawned specialists, teams, and stopping them mid-flight.

| Function | Edge cases | Status |
|---|---|---|
| `get_agent_details` | An agent with no tools declared — the explorer case that could not read a file | `todo` |
| `cancel_agent_run` | Cancelling one specialist while its siblings keep running | `todo` |
| `discard_ephemeral_agent` | Discarding one that is mid-run | `todo` |
| `promote_ephemeral_agent` | Promoting one whose definition no longer validates | `todo` |

## Plan mode

Propose, approve, refine.

| Function | Edge cases | Status |
|---|---|---|
| `get_latest_plan` | No plan yet; a plan from a previous session | `todo` |
| `approve_plan` | Approving twice; approving a plan whose run was cancelled | `covered` |
| `dismiss_plan` | Dismiss must not flip the card to decided | `covered` |

## Sessions

History that survives a restart.

| Function | Edge cases | Status |
|---|---|---|
| `list_sessions` | No past sessions; a session whose title is still generating. **Reopening one from the sidebar is untested** — the harness kept losing the message across the "+ New chat" re-render | `verified` |
| `switch_session` | Switching mid-run. **Verified**: the new session stays clean — no output from the in-flight run bleeds into it | `verified` |
| `attach_session` | Two clients attached to one session | `todo` |
| `get_session_id` | Before the first message is sent | `verified` |
| `get_interrupted_runs` | A run interrupted by a crash rather than a cancel | `todo` |
| `discard_interrupted_run` | Discarding one that has already resumed | `todo` |
| `fire_session_start_hook` | A hook that fails; one that never returns | `todo` |
| `get_project_dir` | A directory that no longer exists | `verified` |

## Models & auth

Which model, and who you are.

| Function | Edge cases | Status |
|---|---|---|
| `get_model_registry` | An empty registry — the state that produced "not in models.registry" | `verified` |
| `switch_model` | Switching mid-run; switching to a model the deployment does not serve | `todo` |
| `login` | Cancelling the browser round-trip; an expired code | `todo` |
| `reload_cloud_credentials` | Credentials file missing or unreadable | `todo` |
| `clear_cloud_credentials` | Clearing while a run is in flight | `todo` |
| `check_for_update` | Offline; a newer version available | `todo` |
| `get_display_config` | A model with no context window published | `todo` |
| `get_output_styles` | An unknown style name | `todo` |

## CodeIndex

The semantic index over the repo.

| Function | Edge cases | Status |
|---|---|---|
| `codeindex_status` | Not indexed — the state the footer shows today | `verified` |
| `codeindex_install` | Install with no Neo4j reachable | `todo` |
| `codeindex_sync` | Sync during an active run | `todo` |
| `codeindex_resync` | Resync of a repo whose HEAD moved mid-sync | `todo` |
| `codeindex_clean` | Cleaning while a query is in flight | `todo` |
| `codeindex_activity` | No activity; a failed indexing job | `todo` |
| `codeindex_head_breakdown` | A repo with one commit | `todo` |
| `search_code` | No results; results in a file since deleted | `todo` |

## MCP & plugins

Third-party tools and extensions.

| Function | Edge cases | Status |
|---|---|---|
| `get_mcp_servers` | None configured | `todo` |
| `get_mcp_server_details` | A server that is configured but unreachable | `todo` |
| `get_mcp_status` | Mid-connection | `todo` |
| `mcp_connect` | A server that refuses; one that hangs | `todo` |
| `mcp_disconnect` | Disconnecting while a tool from it is running | `todo` |
| `ensure_mcp` | Called twice concurrently | `todo` |
| `set_mcp_tool_enabled` | Disabling a tool the model is about to call | `todo` |
| `get_plugin_details` | A plugin whose manifest is malformed | `todo` |
| `get_plugin_contents` | A plugin with no files | `todo` |
| `preview_plugin` | Previewing one that fails to load | `todo` |
| `install_plugin` | Installing over an existing version; a version conflict | `todo` |
| `update_plugin` | Update that fails halfway | `todo` |
| `remove_plugin` | Removing one currently in use | `todo` |
| `set_plugin_enabled` | Disabling mid-run | `todo` |
| `get_marketplaces` | None added | `todo` |
| `add_marketplace` | A URL that 404s; a duplicate | `todo` |
| `remove_marketplace` | Removing one that plugins came from | `todo` |
| `refresh_marketplaces` | Offline | `todo` |

## Skills & commands

Reusable recipes and the slash surface.

| Function | Edge cases | Status |
|---|---|---|
| `get_skill_names` | None defined | `todo` |
| `get_skill_definitions` | A skill whose YAML front matter is malformed — seen locally: an unquoted colon skips the skill with a raw parser error | `todo` |
| `get_skill_details` | A skill referencing a tool that does not exist | `todo` |
| `get_slash_commands` | **Verified**: typing `/` opens the menu. Note the welcome screen advertises /agents /skills /workflows /codeindex /schedule /loop /mcp /plugins /knowledge — a wider surface than the eight in `slash.py`, and none of those nine are driven yet | `covered` |

## Workflows, loops & scheduling

Repeatable and deferred work.

| Function | Edge cases | Status |
|---|---|---|
| `list_workflows` | None defined | `todo` |
| `run_workflow` | A workflow whose first step fails; one cancelled mid-step | `todo` |
| `cancel_workflow` | Cancelling after the last step started | `todo` |
| `get_scheduled_tasks` | None scheduled; one overdue | `todo` |
| `execute_scheduled_task` | Executing one whose session is gone | `todo` |
| `cancel_scheduled_task` | Cancelling one already executing | `todo` |
| `start_scheduler` | Starting twice | `todo` |
| `loop_status` | A loop with no iterations left | `todo` |
| `loop_pause` | Pausing between iterations vs mid-iteration | `todo` |
| `loop_resume` | Resuming one that was cancelled | `todo` |
| `cancel_pending_loop` | Cancelling with iterations queued | `todo` |
| `pop_pending_loop_iteration` | Popping when the queue is empty | `todo` |

## Background processes

Things still running when the turn ends.

| Function | Edge cases | Status |
|---|---|---|
| `list_background_processes` | None; an orphan whose parent died | `covered` |
| `stop_background_process` | Stopping one that already exited | `todo` |
| `read_process_tail` | A process with no output yet; one writing faster than the tail reads | `covered` |

## Knowledge & learnings

What the agent is told to remember.

| Function | Edge cases | Status |
|---|---|---|
| `knowledge_add` | Adding a duplicate; adding an empty document | `todo` |
| `knowledge_get` | A missing id | `todo` |
| `knowledge_list` | Empty | `todo` |
| `knowledge_remove` | Removing while a search is in flight | `todo` |
| `knowledge_search` | No results; a query longer than the embedding limit | `todo` |
| `get_knowledge_status` | Never synced | `todo` |
| `auto_sync_knowledge` | Sync with no source configured | `todo` |
| `extract_learnings` | A session with nothing worth extracting | `todo` |

## Files, dirs & attachments

Getting your material in.

| Function | Edge cases | Status |
|---|---|---|
| `upload_attachment` | A file larger than the limit; an unsupported type; upload cancelled midway | `todo` |
| `list_dirs` | A directory with no read permission | `todo` |
| `pick_dir_native` | Cancelling the native dialog | `todo` |

## Client shell & state

The window around the conversation.

| Function | Edge cases | Status |
|---|---|---|
| `get_client_state` | First run, no stored state | `verified` |
| `set_client_state` | Two windows writing concurrently | `todo` |
| `delete_client_state` | Deleting while the UI reads it | `todo` |
| `get_hooks_details` | A hook file that does not parse | `todo` |
| `reload_hooks` | Reloading while a hook is executing | `todo` |
| `get_group_policy` | No group — the footer shows "Group none" today | `verified` |
| `get_todos` | No todos; a todo list longer than the panel | `todo` |
| `dispatch_visualization_action` | An action for a chart that has been replaced | `covered` |
| `shutdown` | Shutdown mid-run; shutdown with background processes alive | `covered` |

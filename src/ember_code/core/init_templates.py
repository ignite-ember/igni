"""Bundled string templates for project initialization.

Extracted from ``init.py`` (iter 43) — the 300+ lines of hook
scripts and template files were dominating the file and had
nothing to do with the initialisation logic. Keeping them here
means edits to a hook script don't force a re-review of the
init-flow code and vice versa.
"""

from __future__ import annotations

# ── Built-in hook scripts ─────────────────────────────────────────────

PRE_PR_REVIEW_HOOK = """\
#!/bin/bash
# .ember/hooks/pre-pr-review.sh
# Hook: PreToolUse (matcher: Bash)
#
# Early warning before push/PR: detects TODOs, debug statements, and
# console.log in staged changes. Informs the AI so it can fix them
# before the push proceeds.

# Read payload from stdin
payload=$(cat)
cmd=$(echo "$payload" | grep -o '"command"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"command"[[:space:]]*:[[:space:]]*"//;s/"$//')

# Only check push/PR commands
case "$cmd" in
  *"git push"*|*"gh pr create"*|*"gh pr"*) ;;
  *) echo '{"continue": true}'; exit 0 ;;
esac

# Check for leftover debug/TODO in staged changes
diff_output=$(git diff --cached 2>/dev/null || git diff HEAD 2>/dev/null)
issues=()

todo_count=$(echo "$diff_output" | grep "^+" | grep -c -i "TODO\\|FIXME\\|HACK\\|XXX" || true)
todo_count=$(echo "$todo_count" | tr -d '[:space:]')
[[ "$todo_count" -gt 0 ]] 2>/dev/null && issues+=("$todo_count TODO/FIXME comment(s)")

debug_count=$(echo "$diff_output" | grep "^+" | grep -c "console\\.log\\|debugger\\|breakpoint()\\|import pdb\\|print(" || true)
debug_count=$(echo "$debug_count" | tr -d '[:space:]')
[[ "$debug_count" -gt 0 ]] 2>/dev/null && issues+=("$debug_count debug statement(s)")

if [[ ${#issues[@]} -eq 0 ]]; then
  echo '{"continue": true}'
  exit 0
fi

msg=$(IFS=", "; echo "${issues[*]}")
echo "{\\"continue\\": true, \\"systemMessage\\": \\"Before pushing: found ${msg} in your changes. Review and fix these issues before proceeding with the push.\\"}"
exit 0
"""

POST_COMMIT_TODO_HOOK = """\
#!/bin/bash
# .ember/hooks/post-commit-todo.sh
# Hook: PostToolUse (matcher: Bash, background: true)
#
# After a git commit, feeds the commit context to the AI so it can
# intelligently update .ember/TODO.md — crossing out completed items
# and adding new ones based on what the commit actually did.

payload=$(cat)
cmd=$(echo "$payload" | grep -o '"command"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"command"[[:space:]]*:[[:space:]]*"//;s/"$//')

# Only act on commit commands
case "$cmd" in
  *"git commit"*) ;;
  *) echo '{"continue": true}'; exit 0 ;;
esac

# Only if TODO.md exists
if [[ ! -f ".ember/TODO.md" ]]; then
  echo '{"continue": true}'
  exit 0
fi

# Gather commit context
commit_msg=$(git log -1 --pretty=format:"%s" 2>/dev/null)
files_changed=$(git diff HEAD~1..HEAD --stat 2>/dev/null | head -30)
diff_preview=$(git diff HEAD~1..HEAD 2>/dev/null | head -200)

# Build the system message
msg="A git commit was just made. Review it and update .ember/TODO.md:\\n"
msg+="- Mark completed items as done (change '- [ ]' to '- [x]')\\n"
msg+="- Add new items if the commit introduced incomplete work\\n"
msg+="- Remove items that are no longer relevant\\n\\n"
msg+="Commit: ${commit_msg}\\n\\n"
msg+="Files changed:\\n${files_changed}\\n\\n"
msg+="Diff preview:\\n${diff_preview}"

# Use python to safely JSON-encode the message
escaped=$(python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))" <<< "$msg")

echo "{\\"continue\\": true, \\"systemMessage\\": ${escaped}}"
exit 0
"""


# ── Starter ember.md template ─────────────────────────────────────────

EMBER_MD_TEMPLATE = """\
# Project Context

<!-- This file gives igni agents context about your project.
     Edit it to match your project's specifics. Agents read this file
     before every task to understand conventions, architecture, and
     domain terminology. -->

## Overview

<!-- Brief description of what this project does. -->

## Tech Stack

<!-- Languages, frameworks, key libraries. -->

## Architecture

<!-- High-level structure: key directories, module boundaries, data flow. -->

## Conventions

<!-- Naming, formatting, patterns the team follows. -->

## Domain Terminology

<!-- Project-specific terms and their meanings. -->
"""


CONFIG_YAML_HEADER = """\
# igni — user configuration
# This file lives at ~/.ember/config.yaml and is never committed to git.
# Project-level overrides go in .ember/config.yaml inside your repo.
# See https://docs.ignite-ember.sh/configuration for details.

"""

PROJECT_CONFIG_TEMPLATE = """\
# igni — project configuration
# This file can be committed to git. Team members share these settings.
# User-level overrides go in ~/.ember/config.yaml.
# See https://docs.ignite-ember.sh/configuration for details.

# models:
#   default: MiniMax-M2.7        # Default model for this project

guardrails:
  pii_detection: true             # Warn on PII in user messages
  # prompt_injection: false       # Warn on prompt injection patterns

knowledge:
  enabled: true                   # Weaviate-backed knowledge base
  collection_name: ember_knowledge

learning:
  enabled: true                   # Learn user preferences, project context, entities across sessions

# orchestration:
#   max_nesting_depth: 5          # Max recursive sub-team levels
#   max_total_agents: 20          # Max agents per request
#   sub_team_timeout: 600         # Sub-team kill timeout (seconds)
"""


_HOME_CONFIG_BOOTSTRAP = """\
# Personal overrides — only what differs from package defaults belongs
# here. Hosted models come from cloud discovery on session start (see
# https://docs.ignite-ember.sh/configuration) so you don't need to
# declare them; this file is for your own additions:
#
# models:
#   # Pin a different default than the first cloud model:
#   # default: gpt-4o
#   registry:
#     # Your own provider — uses an env-var-resolved API key:
#     # gpt-4o:
#     #   provider: openai_like
#     #   model_id: gpt-4o
#     #   api_key_env: OPENAI_API_KEY
"""

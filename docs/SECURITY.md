# Security

igni follows a defense-in-depth approach. Multiple independent layers of protection ensure that even if one layer is bypassed, others still guard against harm.

## Threat Model

igni is an AI agent that reads, writes, and executes code on behalf of the user. The primary threats:

| Threat | Risk | Mitigation |
|---|---|---|
| **Destructive commands** | Agent runs `rm -rf /`, `DROP TABLE`, force-push | Blocked patterns, confirmation prompts |
| **Sensitive file access** | Agent reads/writes `.env`, credentials, private keys | Protected paths list, file guards |
| **Prompt injection** | Malicious content in files/URLs tricks the agent | Tool output filtering, agent isolation |
| **Runaway recursion** | Agents spawn infinite sub-teams | Depth limits, agent caps, timeouts |
| **Data exfiltration** | Agent sends code to external services | Network restrictions, web access controls |
| **Supply chain** | Malicious agents or MCP servers | Agent loading from trusted directories, MCP approval |
| **Cost runaway** | Agent makes excessive API calls | Token limits, rate limiting, max tool calls |

---

## Security Layers

### 1. Permission System

Every tool call goes through permission checks before execution.

**Permission levels:**

| Level | Behavior | When to Use |
|---|---|---|
| `allow` | Auto-approve, no prompt | Read-only operations, safe commands |
| `ask` | Prompt user for approval | File writes, shell commands, git push |
| `deny` | Block entirely | Web access in sensitive projects |

**Default permissions:**

```yaml
permissions:
  file_read: "allow"
  file_write: "ask"
  shell_execute: "ask"
  shell_restricted: "allow"       # read-only commands (rg, find, tree)
  web_search: "deny"
  web_fetch: "deny"
  git_push: "ask"
  git_destructive: "ask"          # force-push, reset --hard, etc.
```

**Approval prompts:**

When a tool call requires approval (`ask` level), the user sees the full command and chooses a response:

```
 ◆ editor wants to run: npm test

  [y] Yes, allow once             — approve this specific invocation
  [a] Always allow                — permanently allow "npm test"
  [s] Allow similar               — permanently allow "npm *" (pattern)
  [n] No, deny                    — block this invocation

  Choice:
```

| Response | Persists | What It Does |
|---|---|---|
| Allow once | No | Approves only this exact call |
| Always allow | Yes | Adds this exact command to the allowlist |
| Allow similar | Yes | Adds a pattern (e.g., `npm *`, `pytest *`) to the allowlist |
| Deny | No | Blocks this call, agent can try alternatives |

Permanent rules are saved to `~/.ember/permissions.yaml`:

```yaml
# Auto-generated from approval prompts — edit to adjust
allowlist:
  shell_execute:
    - "npm test"           # from "always allow"
    - "pytest *"           # from "allow similar"
    - "ruff check *"       # from "allow similar"
  file_write:
    - "src/**"
    - "tests/**"
```

Pattern matching uses glob syntax. Exact entries come from "always allow", pattern entries from "allow similar". When a new command comes in, it checks the allowlist first, then falls back to the permission level from config.

**Permission modes (CLI shortcuts):**

| Mode | What It Does |
|---|---|
| `ignite-ember` (default) | Asks for writes and shell commands |
| `ignite-ember --accept-edits` | Auto-approves file edits, asks for shell |
| `ignite-ember --strict` | Asks for everything including reads |
| `ignite-ember --read-only` | No file modifications allowed |
| `ignite-ember --auto-approve` | Auto-approves everything (use with caution) |

### 2. Protected Paths

Files matching these patterns **cannot be written to**, regardless of permission level:

```yaml
safety:
  protected_paths:
    - ".env"
    - ".env.*"
    - "*.pem"
    - "*.key"
    - "credentials.*"
    - "secrets.*"
    - "id_rsa*"
    - "*.p12"
    - "*.pfx"
```

Protected paths are enforced at the tool layer — even if an agent tries to write to `.env`, the tool refuses. The agent is informed why and can suggest alternatives.

Add project-specific protected paths:

```yaml
# .ember/config.yaml
safety:
  protected_paths:
    - ".env"
    - "production.config.yaml"
    - "terraform/*.tfstate"
    - "keys/"
```

### 3. Command Blocking

**Blocked commands** (always blocked):

```yaml
safety:
  blocked_commands:
    - "rm -rf /"
    - ":(){ :|:& };:"          # fork bomb
    - "mkfs"
    - "dd if=/dev/"
    - "> /dev/sda"
```

**Confirmation-required commands:**

```yaml
safety:
  require_confirmation:
    - "git push"
    - "git push --force"
    - "npm publish"
    - "pip install"
    - "docker run"
    - "terraform apply"
    - "kubectl apply"
    - "kubectl delete"
```

### 4. Agent Isolation

Each agent only gets the tools declared in its `.md` definition:

```yaml
# explorer.md — read-only tools only
tools: Glob, Grep, LS, Read, WebSearch
```

An explorer agent **cannot** call Write, Edit, or Bash — even if it tries. The tool registry enforces this at instantiation time. This means:

- A compromised agent prompt can't escalate to destructive tools
- Read-only agents are genuinely read-only
- You control the blast radius per agent

### 5. Orchestration Guards

Recursive sub-team spawning has configurable limits:

```yaml
orchestration:
  max_nesting_depth: 5           # max recursive sub-team levels
  max_total_agents: 20           # max agents per request
  sub_team_timeout: 120          # seconds before sub-team times out
```

If limits are reached, the agent is informed and must proceed without sub-teams. These prevent:
- Infinite recursion (agent spawns agent spawns agent...)
- Resource exhaustion (too many concurrent agents)
- Hanging sub-teams (timeout kills stalled work)

### 6. Audit Logging

Every tool execution is logged:

```yaml
storage:
  audit_log: "~/.ember/audit.log"
```

**Log format:**
```
2026-03-13T10:30:00Z | session:abc123 | agent:editor | tool:Edit | path:/src/auth.py | status:success
2026-03-13T10:30:01Z | session:abc123 | agent:editor | tool:Bash | cmd:pytest | status:success
2026-03-13T10:30:02Z | session:abc123 | agent:editor | tool:Write | path:.env | status:BLOCKED (protected)
```

Audit logs are useful for:
- Post-incident investigation
- Compliance requirements
- Understanding what the agent did and why
- Detecting anomalous tool usage patterns

### 7. MCP Security

**As server (IDE integration):**
- **stdio only** — no network exposure; only the parent process connects
- Permission system applies to all MCP tool calls
- Audit logging covers MCP calls
- MCP servers igni consumes are NOT exposed to IDE clients (no passthrough)

**As client (consuming external servers):**
- Project-scoped servers require approval on first use
- Environment variables keep secrets out of config files
- Tool filtering limits which agents access which MCP tools
- Each MCP server runs in its own process (isolation)

### 8. Hooks Security

Hooks extend the security model — they can add custom validation:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "type": "command",
        "command": ".ember/hooks/security-check.sh",
        "matcher": "Write|Edit|Bash"
      }
    ]
  }
}
```

See [Hooks](HOOKS.md) for examples: blocking destructive commands, protecting sensitive paths, enforcing test execution.

### 9. Guardrails (AI Safety)

Built-in safety guardrails run as Agno pre-hooks before each agent turn, catching problems before they reach the model:

| Guardrail | What It Does | When It Triggers |
|---|---|---|
| **PII Detection** | Scans prompts for personally identifiable information (emails, phone numbers, SSNs, etc.) | Before model call — flags PII so the agent can redact or warn |
| **Prompt Injection** | Detects injection attempts in user input and tool output (e.g., "ignore previous instructions") | Before model call — blocks the injected content |
| **Moderation** | Content moderation via OpenAI's moderation API | Before model call — flags harmful content |

```yaml
guardrails:
  pii_detection: true          # detect and flag PII in prompts
  prompt_injection: true       # detect injection attempts
  moderation: true             # OpenAI moderation API
```

Guardrails are applied via `AgnoFeatures.apply_to_agent()` — they attach as pre-hooks that execute before the model is called. If a guardrail triggers, the agent is informed and can adjust its approach. Guardrails work alongside (not instead of) the permission system and protected paths.

**Enterprise use:** Guardrails are especially valuable for teams where agents process user-provided content (URLs, files, pasted text) that could contain injection attempts or sensitive data.

---

## Enterprise Hardening

### Managed Configuration

For organizations, administrators can enforce security policies that individual users cannot override:

```json
// /Library/Application Support/EmberCode/managed-settings.json (macOS)
// /etc/ignite-ember/managed-settings.json (Linux)
{
  "permissions": {
    "web_search": "deny",
    "web_fetch": "deny",
    "git_destructive": "deny"
  },
  "safety": {
    "protected_paths": [
      ".env*",
      "*.pem",
      "*.key",
      "terraform/*.tfstate"
    ],
    "blocked_commands": [
      "curl",
      "wget",
      "nc"
    ]
  },
  "models": {
    "allowed": ["MiniMax-M2.7"]
  }
}
```

Managed settings:
- Cannot be overridden by user or project config
- Applied before any other config layer
- Set by IT/security teams via MDM or system configuration

### Managed MCP Servers

Control which MCP servers are allowed or required:

```json
{
  "mcp": {
    "required": ["corporate-tools"],
    "allowed": ["corporate-tools", "playwright", "github"],
    "denied": ["*-untrusted-*"]
  },
  "mcpServers": {
    "corporate-tools": {
      "type": "http",
      "url": "https://tools.internal.corp.com/mcp"
    }
  }
}
```

### Network Restrictions

```yaml
safety:
  network:
    allowed_domains:
      - "api.ignite-ember.sh"    # All Ember services (models, CodeIndex, embeddings)
      - "github.com"
    block_all_other: true          # deny-by-default for network access
```

### CodeIndex Data Residency

For teams that can't send code to the cloud, self-hosted CodeIndex is planned for a future release. This will allow all code analysis and embeddings to stay on your infrastructure. See [CodeIndex](CODEINDEX.md#self-hosting-planned) for details.

---

## Comparison with Claude Code

| Security Feature | Claude Code | igni |
|---|---|---|
| Permission tiers | allow / ask / deny per tool | Same, plus category-based presets |
| Protected paths | Via deny rules | Dedicated protected_paths list |
| Command blocking | macOS sandbox (Seatbelt) | Blocked patterns, confirmation prompts |
| Audit logging | Not built-in | Built-in to `~/.ember/audit.log` |
| Agent isolation | Tools per agent definition | Same — tools declared in `.md` |
| Depth limits | Sub-agents capped at 1 level | Configurable: depth, agent count, timeout |
| Managed settings | Enterprise policy files | Same — managed-settings.json |
| MCP security | Approval prompts, no passthrough | Same + tool filtering per agent |
| Hooks | PreToolUse/PostToolUse/Stop | Same events, same format |
| Guardrails | Not built-in | PII detection, prompt injection, moderation pre-hooks |
| Network control | Not built-in | allowed_domains, deny-by-default |
| CodeIndex | N/A | Self-hosting planned for data residency |

---

## Security Checklist

### For Individual Users

- [ ] Review default permissions — tighten if working with sensitive code
- [ ] Add project-specific protected paths for credentials, configs, state files
- [ ] Enable `require_confirmation` for destructive operations
- [ ] Review MCP servers before approving — understand what tools they provide
- [ ] Check audit log periodically: `~/.ember/audit.log`

### For Teams

- [ ] Commit `.ember/config.yaml` with team-agreed permission levels
- [ ] Add protected paths for production configs, secrets, infrastructure state
- [ ] Set up hooks for additional validation (security scanning, test enforcement)
- [ ] Use agent evals to verify agents don't escalate permissions after changes

### For Enterprise

- [ ] Deploy managed-settings.json via MDM/configuration management
- [ ] Self-host CodeIndex for code data residency (planned — coming soon)
- [ ] Restrict allowed MCP servers to approved list
- [ ] Enable network deny-by-default with whitelisted domains
- [ ] Review blocked command lists and confirmation requirements
- [ ] Set up audit log aggregation for compliance

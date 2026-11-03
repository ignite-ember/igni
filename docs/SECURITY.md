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
| **A web page or local process driving the backend** | Browsers apply no CORS to WebSockets: any page could open `ws://127.0.0.1:<port>` and run tools | Origin allowlist and a per-launch token at the handshake (see [10](#10-the-backends-local-websocket)) |

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
  file_read: "allow"             # no effect (see below)
  file_write: "ask"
  shell_execute: "ask"
  shell_restricted: "allow"       # no effect (see below)
  web_search: "ask"               # a request out of your network: asked each time
  web_fetch: "ask"
  git_push: "ask"
  git_destructive: "ask"          # force-push, reset --hard, etc.
```

`file_read` and `shell_restricted` are accepted and read by nothing, on
purpose. igni has no read tool — a file is read with `run_shell_command` — so
in the default mode `cat`, `ls` and `rg` ask like any other command, and a
headless `igni -m` cannot read a file unless something allows it. To let reads
run unasked, add allow rules (`permissions.allow`, or "Allow similar" at the
prompt), or set `permissions.mode: plan`, where a command runs without asking
only if it is built entirely from read-only commands (`rg`, `cat`, `ls`, `git
diff`, …) and anything else is put to you. The default mode does not make that
exemption: a read nobody sees, next to `web_fetch`, is how a file leaves the
machine.

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

Permanent rules are saved to `~/.igni/permissions.yaml`:

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

**A rule covers one command.** A line of several commands (`a && b`, `a; b`,
`a | b`) is allowed only when every command on it is, and deny and ask rules
match any command on it: `Bash(rm *)` catches `cd build && rm -rf .`. One
exception: a pure filter directly after a `|` needs no rule of its own —
`head`, `tail`, `wc`, `sort`, `uniq`, `grep`/`egrep`/`fgrep`, `rg`, `cut`,
`tr`, `nl`, `jq` — as long as it only reads its input: no file operand, and no
option that writes (`sort -o`, `uniq IN OUT`), follows (`tail -f`), recurses
(`grep -r`), reads another file (`grep -f`, `jq --rawfile`) or runs a program
(`rg --pre`, `rg -z`, `sort --compress-program`). So `Bash(pytest *)` allows
`pytest -q 2>&1 | tail -20`, while `ls | tee x`, `cat x | sh` and
`ls | xargs rm` still need rules for `tee`, `sh` and `xargs`. The list is
fixed (`permission_eval/pipe_filters.py`); `tee`, `xargs`, `sed`, `awk` and the
pagers `less`/`more` are not on it. The command before the first `|` always
needs its rule, and a deny or ask rule naming a filter still applies to it.

**Permission modes (CLI shortcuts):**

| Mode | What It Does |
|---|---|
| `ignite-ember` (default) | Asks for writes and shell commands |
| `ignite-ember --accept-edits` | Auto-approves file edits, asks for shell |
| `ignite-ember --strict` | Denies anything without an explicit allow rule, including reads — it never asks about the agent's tools. Hooks your organisation's group policy ships run only once you approve them (`hooks.group: ask`), and not at all headless |
| `ignite-ember --read-only` | Denies the write tools **and the shell** — a hard wall (see below) |
| `ignite-ember --auto-approve` | Auto-approves everything (use with caution) |

#### The last check before `--auto-approve` runs a call

Bypass (`--auto-approve`) still checks every non-read call before it runs:
rules for actions that are dangerous by their shape (deleting outside the
project, `curl | sh`, force-pushing, sending credentials off the machine…),
then Laya, a small typed-decision model, for what no rule names. A call
either flags is turned back into the ordinary approval prompt.

Laya does not run inside the backend. It runs in **igni's decisions
service** (`python -m ember_code.decisions_server`), which brings torch and
the model's checkpoints:

- **On a desktop** the backend starts it on first use as its own process,
  on a loopback port, with a secret generated for that run; it exits when
  the backend exits, or after 15 minutes unused, and is started again when
  next needed. The desktop app installs it (`ignite-ember[decisions]`).
- **In the cloud** one service runs per node and every backend on the node
  points `IGNI_DECISIONS_URL` at it, sending `IGNI_DECISIONS_TOKEN` (the
  service's `IGNI_DECISIONS_SECRET`). The service answers approvals before
  anything an extension asks, across every backend it serves.

**It fails closed.** If the service cannot be started or reached, or the
model cannot load — including an install without the `decisions` extra — the
check cannot run, and a call that would have been checked is *asked about*,
never let through. A headless run with nobody to ask refuses it with the
reason.

#### What `--read-only` costs

It denies the write tools — `save_file`, `create_file`, `edit_file`,
`edit_file_replace_all` and the notebook mutations — **and `run_shell_command`**.

Denying the shell is the deliberate half. igni has no read tool: a file is read
with `run_shell_command "cat <path>"`. So `--read-only` cannot read your working
tree, and that is the price of the guarantee. Leaving the shell open would mean
`>`, `sed -i` and `git checkout` still write, which makes the flag an honour
system rather than a wall — and a wall is the thing you reach for this flag to
get.

What still works is analysis that does not route through the shell:

- an extension's read-only tools — CodeIndex's `codeindex_cypher` queries the
  code graph, and is the intended way to explore a codebase in this mode;
- the notebook readers;
- `web_search` and `fetch_url`.

The practical consequence: `--read-only` is a strong mode **when CodeIndex is
populated** and a thin one when it is not. If you want an agent that can read
files but not edit them, do not use this flag — run without it and answer the
write prompts, or use `--accept-edits` against a scratch copy.

When a shell call is denied the model is told so explicitly, along with the
alternatives, rather than receiving a bare refusal it will try to work around.

In a headless run (`-m` / `--pipe`) the agent submits its plan and the CLI
prints it before exiting, since there is no plan card to approve and no second
turn in which an approval could take effect.

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
# .igni/config.yaml
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
  audit_log: "~/.igni/audit.log"
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
        "command": ".igni/hooks/security-check.sh",
        "matcher": "Write|Edit|Bash"
      }
    ]
  }
}
```

See [Hooks](HOOKS.md) for examples: blocking destructive commands, protecting sensitive paths, enforcing test execution.

Hooks also run code. Your organisation's group policy can ship hooks, and they run on every member's machine outside the agent's permissions, so `--strict` does not stop them on its own. `hooks.group` (`run` / `ask` / `refuse`, default `run`) is your control over them. Only your own `~/.igni/config.yaml` and the command line set it, never the group policy, the managed file or a project. `--strict` sets at least `ask`, and in a headless run `ask` means a group hook you have not approved does not run. See [Your organisation's hooks](HOOKS.md#your-organisations-hooks-hooksgroup).

### Workspace trust

Opening a folder never runs its code or loosens safety until the person trusts it: an untrusted folder's tools, hooks, workflows, language servers, monitors, plugins and markdown-command shell tokens do not run, and its config cannot set `permissions.*`, `safety.*` or the mode, nor send a credential to a host it names (no model `api_key_cmd` or `api_key_env`, and a model it re-points keeps no key from your own config). igni server's token goes only to the origin of `api_url`, trusted folder or not. Decisions live in `~/.igni/workspace-trust.json`, keyed by path and git remote. Organisations set `workspace_trust.mode` (`ask` / `trusted_remotes` / `never`) from managed settings or group policy only, and managed permission values are final against every lower tier. See [Workspace trust](CONFIGURATION.md#workspace-trust).

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

### 10. The backend's local WebSocket

The desktop app (and the IDE webviews) talk to the backend over a WebSocket
on `127.0.0.1`, on a port picked at launch and published in
`<project>/.igni/backend.lock`. Loopback keeps the network out, but not the
person's own browser: browsers apply no CORS to WebSockets, so any page they
open could connect and drive an agent that has shell and file access. Two
checks run at the handshake, before the connection is upgraded
(`src/ember_code/transport/ws_auth.py`); a refused connection gets
`403 Forbidden` and a log line that names the reason (`foreign origin`,
`no token`, `wrong token`) but never the token.

**Origin, always.** A browser always sends `Origin`. Allowed: the Tauri
webview (`tauri://localhost`, `http://tauri.localhost`,
`https://tauri.localhost`), VS Code webviews (`vscode-webview://…`) and
loopback pages (`http(s)://127.0.0.1`, `localhost`, `[::1]`, any port: the
JetBrains panel's own server, the Vite dev server). Everything else is
refused, including a DNS-rebound name and `null` (sandboxed iframes,
`file://`). A request without `Origin` comes from a non-browser client.

**A per-launch token, where the launcher made one.** The desktop app
generates 32 random bytes (base64url) at each launch of the backend:

| Step | How | Why that way |
|---|---|---|
| App → backend | `IGNI_WS_TOKEN` in the child's environment; the backend removes it from its own environment at start | Never on the command line, which `ps` shows; never inherited by the agent's shell commands, hooks or MCP servers |
| App → its webview | The URL fragment, `index.html?ws=…#igni_ws_token=…` | A fragment is never sent to a server or written to a request log, and it survives reloads |
| Webview → backend | `Sec-WebSocket-Protocol: igni.v1, igni-token.<token>`; the backend answers `igni.v1` only | The one handshake field a page can set; unlike a query string it ends up in no log |
| Another igni client on the same project | `ws_token` in `.igni/backend.lock`, which is then written owner-only (0600) | A second desktop window or instance attaches to the running backend instead of starting a duplicate |

The backend compares with `hmac.compare_digest` and refuses a connection
without the token, with a wrong one, or from a foreign origin even with the
right one. `X-Igni-Gateway: control` does not stand in for the token: on a
desktop anybody can send that header.

**Where there is no token.** A backend started by hand
(`python -m ember_code.backend --ws-port …`) or by the VS Code or JetBrains
plugin gets no token, so only the Origin check applies: web pages are kept
out, local processes of the same user are not. In the cloud the backend
listens inside the producer's VM, reachable only through the node agent's
vsock stream, and the gateway authenticates every client (an igni bearer
token) before it opens a stream; the gateway's own connections are marked
`X-Igni-Gateway: control` by the node agent, which strips that header from
every other stream. See [CLOUD.md](CLOUD.md).

**Development.** `IGNI_DEV_NO_WS_TOKEN=1` drops the token requirement. The
desktop app honours it only in a debug build (`cargo tauri dev`) and strips
it from the backend's environment in a release build; the backend honours it
with a warning in its log. Never set it on a machine you use for real work.

---

## Enterprise Hardening

### Managed Configuration

For organizations, administrators can enforce security policies that individual users cannot override:

```json
// /Library/Application Support/igni/managed-settings.yaml (macOS)
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

### Outbound connections by default

What an install nobody has configured contacts on its own, and why each one
is on or off. "On request" means only when the person asks for it; `air_gapped:
true` turns every one of these off ([CONFIGURATION.md](CONFIGURATION.md),
"Air-gapped mode").

| Connection | Default | Why |
|---|---|---|
| The model endpoints and igni server you configure | On | The product: nothing is configured, so nothing is contacted until you name one |
| Update check (`pypi.org`) | On, daily | How a fix reaches you: the Cyber Resilience Act asks for security updates to be on by default with an opt-out. `update_check_ttl: 0` turns it off |
| Desktop updater (`github.com/ignite-ember/igni/releases`, or `update_endpoint`) | On (desktop app) | The same reason; it checks with `update_check_ttl` (0 turns it off) and installs only a signed release, when you press Install |
| Desktop first launch (uv, CPython, `ignite-ember`, the decision model) | Once, at install | Installing the app; a pre-provisioned `runtime_python` skips it |
| Decision model (Laya, from Hugging Face) | On first use | Used by Bypass mode's guard and by extensions that ask; loaded from the local cache when it is there |
| Web search (DuckDuckGo) and URL fetch | **Asks each time** | A request the model decides on, out of your network: it waits for your yes. `permissions.web_search` / `web_fetch: allow` lets them run unasked; `deny` removes them |
| Anthropic's plugin marketplace (github.com) | **Off** | `plugins.default_marketplace: true`, or `/plugin marketplace add`, on request |
| Plugin, extension and MCP installs and updates | On request | Only what you install, from where you name |
| Agno's run telemetry | Off | Never on unless `AGNO_TELEMETRY=true` is set, and overruled when air-gapped |
| OpenTelemetry export | Off | Only to a collector an administrator names |

**The web asks, and that is decided, not provisional.** In the normal modes a
web search or URL fetch waits for a person's yes (`--strict` denies it unless a
rule allows it; `air_gapped` builds neither tool). Bypass (`--auto-approve`,
`/bypass`) means "do not ask me", so there both run unasked like every other
tool, keeping what Bypass always keeps: an explicit `deny` still wins, and its
last check still turns a fetch whose URL carries what looks like a secret or
token, or that the safety model flags, back into a prompt.
`tests/test_nothing_phones_home.py` pins both halves.

Nothing goes to Ignite Ember: there is no vendor-operated service in the path.
`tests/test_nothing_phones_home.py` holds the list of hosts the code may name
and fails the build on a new one; `tests/test_an_air_gapped_install_makes_no_outbound_call.py`
proves each row's air-gapped behaviour.

### Network Restrictions

```yaml
safety:
  network:
    allowed_domains:
      - "api.ignite-ember.sh"    # All igni services (models, CodeIndex, embeddings)
      - "github.com"
    block_all_other: true          # deny-by-default for network access
```

### CodeIndex Data Residency

For teams that can't send code to the cloud, self-hosted CodeIndex is planned for a future release. This will allow all code analysis and embeddings to stay on your infrastructure. See *Self-hosting* in the CodeIndex repository's `igni-integration.md` for details.

---

## Comparison with Claude Code

| Security Feature | Claude Code | igni |
|---|---|---|
| Permission tiers | allow / ask / deny per tool | Same, plus category-based presets |
| Protected paths | Via deny rules | Dedicated protected_paths list |
| Command blocking | macOS sandbox (Seatbelt) | Blocked patterns, confirmation prompts |
| Audit logging | Not built-in | Built-in to `~/.igni/audit.log` |
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
- [ ] Check audit log periodically: `~/.igni/audit.log`

### For Teams

- [ ] Commit `.igni/config.yaml` with team-agreed permission levels
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

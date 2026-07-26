// workflow_runtime.mjs
// ─────────────────────────────────────────────────────────────────
// Node bridge for executing .claude/workflows/*.mjs scripts.
//
// Provides the `phase / agent / parallel / pipeline / log` globals
// the script body calls (CC's Workflow() semantics), emits
// structured events on stdout, and bridges agent() calls back to
// the BE via JSON-RPC over stdin.
//
// CLI:
//   node workflow_runtime.mjs <workflow_path> \
//     --workflow-run-id <id> --session-id <sid> --args '<json>'
//
//   node workflow_runtime.mjs <workflow_path> --discovery
//
// Stdout envelope (one JSON per line):
//   {"ts":<ms>,"run_id":"<wid>","seq":<n>,"type":"<event>","payload":{...}}
//
// Stdin envelope (BE → Node, agent call responses):
//   {"type":"agent_response","id":"<uuid>","ok":<bool>,"result":<any>,"error":<str>}
//   {"type":"cancel"}
//
// Agent call requests are emitted on stdout as events of type
// "agent_request"; the BE resolves them and writes the response
// back via stdin.

import { createContext, runInContext, Script } from "node:vm"
import { readFileSync } from "node:fs"
import { argv, cwd, exit, stdout, stdin } from "node:process"

// ── CLI parsing ────────────────────────────────────────────────────

function parseArgs(raw) {
  const args = { _: [] }
  let i = 0
  while (i < raw.length) {
    const a = raw[i]
    if (a.startsWith("--")) {
      const key = a.slice(2)
      const next = raw[i + 1]
      if (next === undefined || next.startsWith("--")) {
        args[key] = true
        i += 1
      } else {
        args[key] = next
        i += 2
      }
    } else {
      args._.push(a)
      i += 1
    }
  }
  return args
}

const cli = parseArgs(argv.slice(2))
const workflowPath = cli._[0]
if (!workflowPath) {
  process.stderr.write("workflow_runtime: missing <workflow_path> argument\n")
  exit(2)
}

const runId = cli["workflow-run-id"] || "wf_local"
const sessionId = cli["session-id"] || ""
const workflowArgs = cli.args ? JSON.parse(cli.args) : {}
const discovery = !!cli.discovery

// ── Event emission ──────────────────────────────────────────────────

let seq = 0
function emit(type, payload) {
  const event = {
    ts: Date.now(),
    run_id: runId,
    seq: seq++,
    type,
    payload,
  }
  stdout.write(JSON.stringify(event) + "\n")
}

// ── Discovery short-circuit ────────────────────────────────────────

if (discovery) {
  try {
    const raw = readFileSync(workflowPath, "utf8")
    // Strip ESM `export` keywords (vm.Script is CommonJS-shaped),
    // then run the whole file inside an async IIFE so the script
    // body can use top-level ``await``, ``phase(...)``, and a
    // trailing ``return <value>`` like the real CC workflow
    // contract.
    const code = raw
      .replace(
        /^\s*export\s+(const|let|var|function|async\s+function|class)\s+/gm,
        "$1 ",
      )
      // The workflow's contract is `export const meta = {...}`.
      // After stripping the `export`, also rebind to
      // ``module.exports.meta`` so the discovery path can read it
      // synchronously without awaiting the IIFE.
      .replace(
        /^\s*(const|let|var)\s+meta\s*=/m,
        "module.exports.meta =",
      )
    const wrapped = `(async () => { ${code}\n })()`
    const mod = { exports: {} }
    const ctx = createContext({ module: mod, exports: mod.exports })
    ctx.module.exports = ctx.exports
    // The IIFE returns a Promise but we don't need the value for
    // discovery — just need ``module.exports.meta`` to be set
    // synchronously by the script before any await. If the script
    // awaits before exporting, the meta is undefined; CC's
    // convention is that meta is set at the top of the file
    // (before any await), so the wait doesn't matter.
    runInContext(wrapped, ctx, { filename: workflowPath })
    const meta = ctx.module.exports?.meta || ctx.exports?.meta
    if (!meta) {
      process.stderr.write(`workflow_runtime: ${workflowPath} did not export 'meta'\n`)
      exit(3)
    }
    emit("workflow_meta", { meta, path: workflowPath })
    exit(0)
  } catch (err) {
    process.stderr.write(`workflow_runtime: discovery failed: ${err.stack || err.message}\n`)
    exit(4)
  }
}

// ── Stdin reader (for agent responses + cancel) ────────────────────

const cancelRequested = { value: false }
const pending = new Map() // id → { resolve, reject }
let stdinBuf = ""

stdin.setEncoding("utf8")
stdin.on("data", (chunk) => {
  stdinBuf += chunk
  let nl
  while ((nl = stdinBuf.indexOf("\n")) >= 0) {
    const line = stdinBuf.slice(0, nl).trim()
    stdinBuf = stdinBuf.slice(nl + 1)
    if (!line) continue
    let msg
    try {
      msg = JSON.parse(line)
    } catch {
      continue
    }
    if (msg.type === "cancel") {
      cancelRequested.value = true
      for (const { reject } of pending.values()) {
        reject(new Error("workflow cancelled"))
      }
      pending.clear()
      continue
    }
    if (msg.type === "agent_response" && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id)
      pending.delete(msg.id)
      if (msg.ok) {
        resolve(msg.result)
      } else {
        reject(new Error(msg.error || "agent call failed"))
      }
    }
  }
})
stdin.on("end", () => {
  for (const { reject } of pending.values()) {
    reject(new Error("subprocess stdin closed"))
  }
  pending.clear()
})

function writeStdin(obj) {
  return new Promise((resolve, reject) => {
    const line = JSON.stringify(obj) + "\n"
    try {
      writeSync(1, line)
      resolve()
    } catch (err) {
      reject(err)
    }
  })
}

// ── Globals provided to the workflow script ────────────────────────

const phaseStack = []

function phase(title) {
  while (phaseStack.length > 0) {
    const prev = phaseStack.pop()
    emit("phase_completed", {
      phase_id: prev.phase_id,
      title: prev.title,
      status: cancelRequested.value ? "cancelled" : "completed",
      duration_ms: Date.now() - prev.started_at_ms,
    })
  }
  const phase_id = `phase_${phaseStack.length + 1}_${Date.now().toString(36)}`
  phaseStack.push({ phase_id, title, started_at_ms: Date.now() })
  emit("phase_started", { phase_id, title })
}

function flushPhases(finalStatus) {
  while (phaseStack.length > 0) {
    const p = phaseStack.pop()
    emit("phase_completed", {
      phase_id: p.phase_id,
      title: p.title,
      status: finalStatus,
      duration_ms: Date.now() - p.started_at_ms,
    })
  }
}

function agent(prompt, opts = {}) {
  const {
    label = "agent",
    phase: agentPhase = null,
    schema = null,
    timeoutSeconds = 600,
  } = opts
  const id = `req_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`
  const started_at_ms = Date.now()
  return new Promise((resolve, reject) => {
    emit("agent_started", {
      id,
      label,
      phase: agentPhase,
      phase_id: phaseStack.length > 0 ? phaseStack[phaseStack.length - 1].phase_id : null,
      prompt_preview: typeof prompt === "string" ? prompt.slice(0, 240) : "",
      schema_keys: schema && typeof schema === "object" ? Object.keys(schema) : null,
    })
    let timer
    if (timeoutSeconds > 0) {
      timer = setTimeout(() => {
        if (pending.has(id)) {
          pending.delete(id)
          emit("agent_completed", {
            id,
            label,
            status: "timeout",
            duration_ms: Date.now() - started_at_ms,
            error: `agent timed out after ${timeoutSeconds}s`,
          })
          reject(new Error(`agent '${label}' timed out after ${timeoutSeconds}s`))
        }
      }, timeoutSeconds * 1000)
    }
    pending.set(id, {
      resolve: (result) => {
        clearTimeout(timer)
        emit("agent_completed", {
          id,
          label,
          status: "completed",
          duration_ms: Date.now() - started_at_ms,
        })
        resolve(result)
      },
      reject: (err) => {
        clearTimeout(timer)
        emit("agent_completed", {
          id,
          label,
          status: "failed",
          duration_ms: Date.now() - started_at_ms,
          error: err.message,
        })
        reject(err)
      },
    })
    emit("agent_request", {
      id,
      prompt,
      label,
      phase: agentPhase,
      timeout_seconds: timeoutSeconds,
    })
  })
}

function parallel(thunks) {
  const group_id = `par_${Date.now().toString(36)}`
  const started_at_ms = Date.now()
  emit("parallel_started", {
    group_id,
    phase_id: phaseStack.length > 0 ? phaseStack[phaseStack.length - 1].phase_id : null,
    lane_count: thunks.length,
  })
  return Promise.all(
    thunks.map((thunk, idx) => {
      const lane_id = `${group_id}_lane_${idx}`
      emit("parallel_lane_started", { group_id, lane_id, index: idx })
      return Promise.resolve()
        .then(() => thunk())
        .then(
          (result) => {
            emit("parallel_lane_completed", {
              group_id,
              lane_id,
              index: idx,
              status: "completed",
            })
            return result
          },
          (err) => {
            emit("parallel_lane_completed", {
              group_id,
              lane_id,
              index: idx,
              status: "failed",
              error: err.message,
            })
            throw err
          },
        )
    }),
  )
    .then((results) => {
      emit("parallel_completed", {
        group_id,
        status: "completed",
        duration_ms: Date.now() - started_at_ms,
      })
      return results
    })
    .catch((err) => {
      emit("parallel_completed", {
        group_id,
        status: "failed",
        duration_ms: Date.now() - started_at_ms,
        error: err.message,
      })
      throw err
    })
}

function pipeline(steps) {
  const group_id = `pipe_${Date.now().toString(36)}`
  const started_at_ms = Date.now()
  emit("pipeline_started", {
    group_id,
    phase_id: phaseStack.length > 0 ? phaseStack[phaseStack.length - 1].phase_id : null,
    step_count: steps.length,
  })
  return steps
    .reduce(
      async (accP, step, idx) => {
        const acc = await accP
        const result = await step(acc)
        emit("pipeline_step_completed", { group_id, index: idx })
        return result
      },
      Promise.resolve(undefined),
    )
    .then(
      (result) => {
        emit("pipeline_completed", {
          group_id,
          status: "completed",
          duration_ms: Date.now() - started_at_ms,
        })
        return result
      },
      (err) => {
        emit("pipeline_completed", {
          group_id,
          status: "failed",
          duration_ms: Date.now() - started_at_ms,
          error: err.message,
        })
        throw err
      },
    )
}

function log(text) {
  emit("log", {
    phase_id: phaseStack.length > 0 ? phaseStack[phaseStack.length - 1].phase_id : null,
    text: String(text),
  })
}

// ── Load + execute the workflow file ───────────────────────────────

let scriptCode = readFileSync(workflowPath, "utf8")
// Strip ESM `export` keywords so vm.Script can run the file in a
// plain context. The workflow's contract is `export const meta = …`
// (used by the discovery path) and the body is plain JS — neither
// uses ESM features that this strip would break.
scriptCode = scriptCode.replace(/^\s*export\s+(const|let|var|function|async\s+function|class)\s+/gm, "$1 ")
const started_at_ms = Date.now()
emit("workflow_started", {
  name: cli.name || null,
  session_id: sessionId,
  args: workflowArgs,
  cwd: cwd(),
})

const ctx = createContext({
  args: workflowArgs,
  phase,
  agent,
  parallel,
  pipeline,
  log,
  console: {
    log: (...parts) =>
      log(
        parts.map((p) => (typeof p === "string" ? p : JSON.stringify(p))).join(" "),
      ),
  },
})

// Always wrap the script in an async IIFE — workflow files use
// top-level `return` (CC convention for the result value) and
// top-level `await`. vm.Script doesn't support either, but a
// thin async arrow-function body does.
const wrapper = `(async () => { ${scriptCode}\n })()`

runInContext(wrapper, ctx, { filename: workflowPath })
  .then(async (result) => {
    if (cancelRequested.value) {
      flushPhases("cancelled")
      emit("workflow_completed", {
        status: "cancelled",
        duration_ms: Date.now() - started_at_ms,
      })
      exit(0)
    }
    flushPhases("completed")
    emit("workflow_completed", {
      status: "completed",
      duration_ms: Date.now() - started_at_ms,
      result: result === undefined ? null : result,
    })
    exit(0)
  })
  .catch((err) => {
    flushPhases("failed")
    emit("workflow_failed", {
      error: err.message,
      stack: err.stack,
      duration_ms: Date.now() - started_at_ms,
    })
    exit(1)
  })

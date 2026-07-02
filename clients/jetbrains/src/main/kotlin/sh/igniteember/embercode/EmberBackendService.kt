package sh.igniteember.embercode

import com.intellij.openapi.Disposable
import com.intellij.openapi.components.Service
import com.intellij.openapi.diagnostic.thisLogger
import com.intellij.openapi.project.Project
import java.io.BufferedReader
import java.io.InputStreamReader
import java.util.concurrent.CompletableFuture
import java.util.concurrent.TimeUnit

/**
 * Project-level service owning the Ember backend process.
 *
 * Resolves a managed Python (see :class:`EmberRuntime`) and spawns
 * `python -m ember_code.backend --ws-port 0 --project-dir <root>`,
 * parsing the JSON ready line for the bound WebSocket port. The
 * process is killed when the project closes (Disposable), and also
 * self-terminates if the IDE dies (EMBER_PARENT_PID watchdog).
 *
 * The user is never asked for a Python interpreter — the runtime
 * downloads ``uv`` on first launch and uses it to provision Python
 * + ``ignite-ember`` into a per-user cache. See ``EmberRuntime`` for
 * the bootstrap details.
 */
@Service(Service.Level.PROJECT)
class EmberBackendService(private val project: Project) : Disposable {

    private val log = thisLogger()
    private var process: Process? = null

    @Volatile
    var wsPort: Int? = null
        private set

    /** The last [EmberRuntime.BackendInstall] that ``ensureStarted``
     *  produced. Captured so the tool-window factory can propagate
     *  the resolved / expected ``ignite-ember`` versions into the
     *  JCEF page URL, letting the web UI render a header chip that
     *  shows the actual running version (and a mismatch warning
     *  when the bootstrap ends up on a stale interpreter). */
    @Volatile
    var lastInstall: EmberRuntime.BackendInstall? = null
        private set

    /** Bootstrap progress hook. The tool window subscribes so the
     *  panel can render "Downloading uv…" / "Installing Python…"
     *  instead of a stale "Starting backend…" while the user waits
     *  on the (possibly multi-minute) first-launch install. */
    @Volatile
    var progressListener: ((String) -> Unit)? = null

    /** Start the backend (idempotent); resolves with the WS port. */
    fun ensureStarted(): CompletableFuture<Int> {
        wsPort?.let { return CompletableFuture.completedFuture(it) }
        val future = CompletableFuture<Int>()
        val projectDir = project.basePath ?: "."

        Thread {
            try {
                progressListener?.invoke("Preparing Ember backend…")
                val install = EmberRuntime.ensureBackendPython { msg ->
                    progressListener?.invoke(msg)
                }
                lastInstall = install
                progressListener?.invoke("Starting Ember backend…")

                val proc = ProcessBuilder(
                    install.python.toString(), "-m", "ember_code.backend",
                    "--ws-port", "0",
                    "--project-dir", projectDir,
                ).apply {
                    environment()["EMBER_PARENT_PID"] = ProcessHandle.current().pid().toString()
                    // HF_HOME / any other runtime-managed env from
                    // ``EmberRuntime`` so the BE process sees the
                    // managed cache instead of ~/.cache/huggingface.
                    environment().putAll(install.env)
                    redirectErrorStream(false)
                }.start()
                process = proc

                val reader = BufferedReader(InputStreamReader(proc.inputStream))
                // Capture stderr separately so we can include it in
                // the error message when the BE dies during startup
                // AND so mid-session warnings end up in idea.log.
                val stderrTail = StringBuilder()
                Thread {
                    try {
                        val errReader = BufferedReader(InputStreamReader(proc.errorStream))
                        var l: String?
                        while (errReader.readLine().also { l = it } != null) {
                            val line = l!!
                            // Mirror BE stderr into idea.log so users
                            // can diagnose problems via Help → Show
                            // Log instead of guessing.
                            log.info("[ember-be] $line")
                            synchronized(stderrTail) {
                                stderrTail.append(line).append('\n')
                                if (stderrTail.length > 4096) {
                                    stderrTail.delete(0, stderrTail.length - 4096)
                                }
                            }
                        }
                    } catch (_: Exception) { /* stream closed — fine */ }
                }.apply {
                    isDaemon = true
                    name = "ember-backend-stderr-drain"
                    start()
                }

                var line: String?
                while (reader.readLine().also { line = it } != null) {
                    val l = line!!.trim()
                    // Ready line: {"status": "ready", "ws_port": N, ...}
                    if (l.startsWith("{") && l.contains("\"ready\"")) {
                        val match = Regex("\"ws_port\"\\s*:\\s*(\\d+)").find(l)
                        val port = match?.groupValues?.get(1)?.toIntOrNull()
                        if (port != null) {
                            wsPort = port
                            future.complete(port)
                            // Keep draining stdout so the BE never blocks.
                            while (reader.readLine() != null) { /* drain */ }
                            return@Thread
                        }
                    }
                }
                // BE exited before announcing ready — give the stderr
                // drainer a beat to catch up, then bubble the tail into
                // the failure message.
                Thread.sleep(200)
                val tail = synchronized(stderrTail) { stderrTail.toString().trim() }
                val detail = if (tail.isNotEmpty()) "\n\nstderr:\n$tail" else ""
                future.completeExceptionally(
                    IllegalStateException(
                        "Ember backend exited during startup.$detail"
                    )
                )
            } catch (e: Exception) {
                log.warn("Ember backend failed to start", e)
                future.completeExceptionally(e)
            }
        }.apply {
            isDaemon = true
            name = "ember-backend-launcher"
        }.start()

        return future
    }

    /** Kill and restart the BE. Used by the "Restart backend" action.
     *  Optionally wipes the managed cache first to recover from a
     *  corrupted install (the action surfaces this as "Reinstall"). */
    fun restart(cleanInstall: Boolean = false): CompletableFuture<Int> {
        process?.let { proc ->
            proc.destroy()
            if (!proc.waitFor(5, TimeUnit.SECONDS)) proc.destroyForcibly()
        }
        process = null
        wsPort = null
        if (cleanInstall) EmberRuntime.resetCache()
        return ensureStarted()
    }

    override fun dispose() {
        process?.let { proc ->
            proc.destroy()
            if (!proc.waitFor(5, TimeUnit.SECONDS)) {
                proc.destroyForcibly()
            }
        }
        process = null
        wsPort = null
    }
}

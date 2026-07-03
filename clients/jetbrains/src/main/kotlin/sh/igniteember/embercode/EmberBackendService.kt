package sh.igniteember.embercode

import com.intellij.notification.NotificationGroupManager
import com.intellij.notification.NotificationType
import com.intellij.openapi.Disposable
import com.intellij.openapi.components.Service
import com.intellij.openapi.diagnostic.thisLogger
import com.intellij.openapi.project.Project
import java.io.BufferedReader
import java.io.IOException
import java.io.InputStreamReader
import java.net.InetSocketAddress
import java.net.Socket
import java.nio.file.Files
import java.nio.file.Paths
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

                // Check for a running BE on this project (typical:
                // user has both JB and VSCode open on the same repo,
                // or two JB windows). Reuse the existing WebSocket
                // instead of spawning a duplicate so both clients
                // share in-memory session state and see each other's
                // chat updates live.
                val discovered = discoverExistingBackend(
                    projectDir = projectDir,
                    expectedWireVersion = install.expectedCliVersion,
                )
                when (discovered) {
                    is DiscoveryResult.Ok -> {
                        wsPort = discovered.port
                        progressListener?.invoke("Reusing running Ember backend on port ${discovered.port}")
                        future.complete(discovered.port)
                        return@Thread
                    }
                    is DiscoveryResult.VersionMismatch -> {
                        val text =
                            "Another igni client is running for this " +
                                "project on version ${discovered.runningVersion}, but " +
                                "this plugin is ${install.expectedCliVersion}. Close " +
                                "the other client (or restart it on the matching " +
                                "version) and reopen the igni tool window."
                        // ``NotificationGroup`` id "igni" is
                        // registered in ``plugin.xml`` — surfaces as
                        // a balloon in the bottom-right so the user
                        // notices without having to open the log.
                        NotificationGroupManager.getInstance()
                            .getNotificationGroup("igni")
                            .createNotification("igni: version mismatch", text, NotificationType.ERROR)
                            .notify(project)
                        future.completeExceptionally(IllegalStateException(text))
                        return@Thread
                    }
                    is DiscoveryResult.Spawn -> { /* fall through */ }
                }

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

    /** Outcome of the ``.ember/backend.lock`` probe. */
    private sealed class DiscoveryResult {
        /** Live BE at ``port`` matches our wire version — connect. */
        data class Ok(val port: Int) : DiscoveryResult()

        /** Live BE at the lockfile but wrong version — refuse. */
        data class VersionMismatch(val runningVersion: String) : DiscoveryResult()

        /** No lock, or stale (dead pid / unreachable port) — spawn. */
        object Spawn : DiscoveryResult()
    }

    /** Read ``<project>/.ember/backend.lock`` and classify. Mirrors
     *  the Python side at ``src/ember_code/backend/lockfile.py``. */
    private fun discoverExistingBackend(
        projectDir: String,
        expectedWireVersion: String,
    ): DiscoveryResult {
        val lockPath = Paths.get(projectDir, ".ember", "backend.lock")
        if (!Files.exists(lockPath)) return DiscoveryResult.Spawn

        val raw = try {
            Files.readString(lockPath)
        } catch (e: IOException) {
            log.info("lockfile read failed: ${e.message}")
            return DiscoveryResult.Spawn
        }
        // Cheap regex JSON parse — the payload is a flat object of
        // three known keys, no need to pull in a JSON library for it.
        val pid = Regex("\"pid\"\\s*:\\s*(\\d+)").find(raw)?.groupValues?.get(1)?.toLongOrNull()
        val port = Regex("\"port\"\\s*:\\s*(\\d+)").find(raw)?.groupValues?.get(1)?.toIntOrNull()
        val version = Regex("\"wire_version\"\\s*:\\s*\"([^\"]+)\"").find(raw)?.groupValues?.get(1)

        if (pid == null || port == null || version == null) {
            log.info("lockfile at $lockPath is malformed; removing")
            try {
                Files.deleteIfExists(lockPath)
            } catch (_: IOException) { /* fine */ }
            return DiscoveryResult.Spawn
        }

        if (!isPidAlive(pid)) {
            log.info("lockfile pid $pid is dead; removing")
            try {
                Files.deleteIfExists(lockPath)
            } catch (_: IOException) { /* fine */ }
            return DiscoveryResult.Spawn
        }

        if (!isPortReachable(port)) {
            log.info("lockfile pid $pid alive but port $port unreachable; removing")
            try {
                Files.deleteIfExists(lockPath)
            } catch (_: IOException) { /* fine */ }
            return DiscoveryResult.Spawn
        }

        if (version != expectedWireVersion) {
            // Keep the lockfile — the running BE is legitimately
            // owned by a different-version client.
            return DiscoveryResult.VersionMismatch(runningVersion = version)
        }
        return DiscoveryResult.Ok(port = port)
    }

    private fun isPidAlive(pid: Long): Boolean {
        return try {
            ProcessHandle.of(pid).map { it.isAlive }.orElse(false)
        } catch (_: Exception) {
            false
        }
    }

    private fun isPortReachable(port: Int, host: String = "127.0.0.1", timeoutMs: Int = 500): Boolean {
        return try {
            Socket().use { s ->
                s.connect(InetSocketAddress(host, port), timeoutMs)
                true
            }
        } catch (_: IOException) {
            false
        }
    }
}

package sh.igniteember.embercode

import com.intellij.openapi.diagnostic.thisLogger
import java.io.File
import java.net.URI
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.StandardCopyOption
import java.time.Duration

/**
 * Owns the lifecycle of the *managed* Ember backend installation.
 *
 * The plugin's goal is "zero-touch": the user installs the plugin
 * and the chat panel works — no `pip install`, no `EMBER_PYTHON`,
 * no virtualenv juggling. To get there we ship nothing about Python
 * in the plugin itself, but on first launch we:
 *
 *   1. Download the ``uv`` binary for the current OS/arch (Astral's
 *      Rust-based Python package manager — single static binary,
 *      ~25 MB, handles Python install + venv + pip).
 *   2. Use ``uv python install`` to fetch a pinned CPython.
 *   3. Use ``uv venv`` to create the backend venv.
 *   4. Use ``uv pip install ignite-ember==<pinned>`` to install the
 *      backend code itself.
 *
 * Everything lives under ``~/.cache/ember-code/`` (or the OS cache
 * dir on Windows). Subsequent launches reuse the cache directly and
 * skip every step above — startup overhead drops to ``uv``'s venv
 * lookup, which is sub-50 ms.
 *
 * **Development override.** If ``EMBER_DEV_BACKEND`` is set, we
 * return that path verbatim and skip the bootstrap entirely. Used
 * during local plugin development to point at an editable install
 * of ``ignite-ember`` (`pip install -e .` in the source tree). Not
 * exposed in the UI; the plugin doesn't have a settings page on
 * purpose.
 */
object EmberRuntime {
    private val log = thisLogger()

    /** Pinned CPython tag fed to ``uv python install``. */
    private const val PYTHON_VERSION = "3.12"

    /** Minimum free bytes the cache filesystem needs before we
     *  start the bootstrap. Sized for ``uv`` (~25 MB) + CPython
     *  (~50 MB) + ``ignite-ember`` + its transitives (chromadb,
     *  sentence-transformers, torch wheels — ~600 MB unpacked) +
     *  the sentence-transformer model (~90 MB) + headroom. 1 GB
     *  is conservative but kind: a disk-full failure midway
     *  through is much worse UX than failing fast here. */
    private const val MIN_BOOTSTRAP_FREE_BYTES = 1024L * 1024L * 1024L

    /** Pinned ``uv`` release used for the runtime bootstrap. Bumped
     *  when we need a new uv feature; otherwise stable. */
    private const val UV_VERSION = "0.5.7"

    /** Pinned ``ignite-ember`` version installed into the managed
     *  venv — read at runtime from the resource ``build.gradle.kts``
     *  generates out of ``pyproject.toml``. Single source of truth
     *  for the version across the Python package + plugin so a
     *  release tag bump flows automatically into the next plugin
     *  build. */
    private val IGNITE_EMBER_VERSION: String by lazy {
        val props = java.util.Properties()
        EmberRuntime::class.java.classLoader
            .getResourceAsStream("META-INF/ember-version.properties")
            ?.use { props.load(it) }
        props.getProperty("ignite-ember-version")
            ?: error("ember-version.properties missing — gradle generateEmberVersion task didn't run")
    }

    /** Marker file that records what's currently installed under the
     *  managed venv. If any of these fields drift from the constants
     *  above we tear the venv down and rebuild — that's the upgrade
     *  path on plugin updates. */
    private const val INSTALL_MARKER = "ember-install.json"

    /** Result of [ensureBackendPython]: the Python interpreter to
     *  spawn the BE with, plus environment variables the caller
     *  should layer onto the BE process. ``HF_HOME`` keeps the
     *  HuggingFace cache inside the plugin-managed directory so
     *  "Reinstall Backend (Clean)" really wipes everything (instead
     *  of leaving ~250 MB of cached embeddings in
     *  ``~/.cache/huggingface``).
     *
     *  ``HTTPS_PROXY`` / ``HTTP_PROXY`` / ``NO_PROXY`` get layered on
     *  too when the IDE has a proxy configured — corporate users
     *  whose shell doesn't know about the IDE's proxy settings would
     *  otherwise see uv / pip / the BE itself fail with cryptic
     *  network errors.
     *
     *  ``actualCliVersion`` is the version string ``import
     *  ember_code; ember_code.__version__`` reports from the chosen
     *  interpreter, captured at bootstrap time so the tool window's
     *  header chip and the Doctor action can render it without
     *  spawning another subprocess. ``null`` when the version probe
     *  fails (dev override at a Python without ``ember_code``
     *  importable, sandbox denies subprocess, etc.). */
    data class BackendInstall(
        val python: Path,
        val env: Map<String, String>,
        val actualCliVersion: String? = null,
        val expectedCliVersion: String,
        val source: BackendSource,
    )

    /** How the interpreter was resolved. Used by the Doctor action
     *  and the header chip to distinguish "you're on the plugin's
     *  managed venv, all fine" from "an env var is redirecting you
     *  somewhere else, watch out." */
    enum class BackendSource { MANAGED_VENV, DEV_OVERRIDE }

    /**
     * Resolve a Python interpreter with ``ignite-ember`` installed
     * AND the sentence-transformer embedding model pre-warmed.
     * Bootstraps on first call; returns from cache thereafter. The
     * ``listener`` receives short human-readable status strings so
     * the tool window can show "Downloading uv…" / "Installing
     * Python…" / "Installing backend…" / "Downloading embedding
     * model…" while the user waits.
     *
     * Blocks the calling thread. Caller should run this on a
     * background thread (``EmberBackendService`` does).
     */
    fun ensureBackendPython(listener: (String) -> Unit): BackendInstall {
        val expected = IGNITE_EMBER_VERSION

        // ── Dev override ──
        // Gated on ``IGNITE_EMBER_DEV=1`` in addition to
        // ``EMBER_DEV_BACKEND`` so an ambient env var left over from
        // an old install (via ``~/.zshenv`` or ``launchctl setenv``)
        // can't silently redirect a regular user to a stale CLI.
        // Devs opt in once (``export IGNITE_EMBER_DEV=1``) and both
        // signals travel together intentionally.
        val devPath = System.getenv("EMBER_DEV_BACKEND")?.takeIf { it.isNotBlank() }
        val devAck = System.getenv("IGNITE_EMBER_DEV")?.takeIf { it == "1" || it.equals("true", ignoreCase = true) }
        if (devPath != null && devAck != null) {
            log.info("EMBER_DEV_BACKEND set; bypassing managed venv: $devPath")
            // Version-check the override before trusting it. If the
            // CLI reachable from that Python doesn't match the pinned
            // version we log a warning and STILL use it — this is
            // dev mode, the whole point is to run against a
            // checkout that might be ahead of/behind the pin. But we
            // record both versions so the header chip can render
            // orange for "override active" and the Doctor action can
            // explain the drift.
            val devActual = probeCliVersion(Path.of(devPath))
            if (devActual != null && devActual != expected) {
                log.warn(
                    "EMBER_DEV_BACKEND at $devPath runs ignite-ember $devActual, " +
                        "plugin pinned to $expected. Continuing (dev mode)."
                )
            }
            return BackendInstall(
                python = Path.of(devPath),
                env = emptyMap(),
                actualCliVersion = devActual,
                expectedCliVersion = expected,
                source = BackendSource.DEV_OVERRIDE,
            )
        }
        if (devPath != null && devAck == null) {
            // ``EMBER_DEV_BACKEND`` is set but the explicit ack env
            // var isn't. This is the footgun path — an old shell
            // config or launchd plist quietly hijacking the plugin's
            // interpreter. Log loudly and fall through to the
            // managed venv so the user gets a working chat instead
            // of a silent stale CLI.
            log.warn(
                "EMBER_DEV_BACKEND=$devPath detected but IGNITE_EMBER_DEV is unset — " +
                    "ignoring override and using the managed venv. " +
                    "Set IGNITE_EMBER_DEV=1 to opt in to the dev-mode override."
            )
        }

        val cache = cacheRoot()
        Files.createDirectories(cache)
        val hfHome = cache.resolve("huggingface")

        // ── Disk-space precondition ──
        // The full bootstrap pulls ~250-300 MB across uv +
        // CPython + ignite-ember + the sentence-transformer model.
        // Bail out NOW with a clean message instead of failing
        // partway through with whatever uv / pip happens to emit
        // when the disk fills.
        ensureFreeSpace(cache, MIN_BOOTSTRAP_FREE_BYTES, listener)

        val markerPath = cache.resolve(INSTALL_MARKER)
        val currentMarker =
            "uv=$UV_VERSION;python=$PYTHON_VERSION;ignite=$IGNITE_EMBER_VERSION"
        val previousMarker = runCatching { Files.readString(markerPath).trim() }.getOrNull()

        val venv = cache.resolve("venv")
        val venvPython = venv.resolve(venvPythonRelPath())

        // Marker-file drift is our primary "reinstall needed"
        // signal, but the marker can lie: it stays put even if the
        // venv was manually pip-upgraded / rolled back / half-
        // installed. Ask the interpreter what version it actually
        // has and treat any mismatch as a marker miss. Cheap
        // (~50 ms subprocess) and catches the entire class of
        // "plugin thinks it's at 0.8.3 but venv is at 0.5.x"
        // reports.
        // Probe the venv's interpreter to catch a specific failure
        // mode: marker file says one version, but the wheels on disk
        // are a different version (manual pip upgrade, half-finished
        // install, plugin update that skipped the marker rewrite,
        // etc.). We only ACT on a positive mismatch — probe returned
        // a version string AND it differs. A null probe means the
        // interpreter is missing or wedged; in that case fall back
        // to the marker/executable-existence signals so a temporary
        // subprocess hiccup doesn't trigger a multi-minute
        // reinstall on every startup.
        val venvActualVersion =
            if (Files.isExecutable(venvPython)) probeCliVersion(venvPython) else null
        val venvVersionMismatch =
            venvActualVersion != null && venvActualVersion != IGNITE_EMBER_VERSION
        val needsReinstall =
            !Files.isExecutable(venvPython) ||
                previousMarker != currentMarker ||
                venvVersionMismatch
        if (venvVersionMismatch && previousMarker == currentMarker) {
            log.warn(
                "Managed venv marker says ignite=$IGNITE_EMBER_VERSION but the " +
                    "interpreter reports $venvActualVersion — reinstalling."
            )
        }

        // ── Step 1: uv binary ──
        val uv = cache.resolve(uvBinName())
        if (!Files.isExecutable(uv) || needsReinstall) {
            listener("Downloading uv (one-time, ~25 MB)…")
            downloadUv(uv)
        }

        // ── Steps 2-4: pinned Python + venv + ignite-ember ──
        if (needsReinstall) {
            // Old venv lingering from a previous plugin version — drop
            // it before re-creating so we don't mix wheels.
            if (Files.exists(venv)) {
                listener("Refreshing managed venv…")
                deleteRecursively(venv)
            }

            listener("Installing Python $PYTHON_VERSION (one-time)…")
            runUv(uv, listOf("python", "install", PYTHON_VERSION))

            listener("Creating backend venv…")
            runUv(uv, listOf("venv", "--python", PYTHON_VERSION, venv.toString()))

            listener("Installing ignite-ember (one-time)…")
            runUv(
                uv,
                listOf(
                    "pip",
                    "install",
                    "--python",
                    venvPython.toString(),
                    "ignite-ember==$IGNITE_EMBER_VERSION",
                ),
            )

            // Pre-warm the sentence-transformer embedding cache so
            // the user's first agent run doesn't stall mid-chat on
            // a silent 90 MB HuggingFace download. ``HF_HOME``
            // points at the managed cache so a clean reinstall
            // catches it too.
            listener("Downloading embedding model (one-time, ~90 MB)…")
            runProcess(
                venvPython,
                listOf("-m", "ember_code.prefetch_models"),
                env = mapOf("HF_HOME" to hfHome.toString()),
            )

            Files.writeString(markerPath, currentMarker)
        }

        // Probe the (possibly-just-reinstalled) venv one more time
        // so the returned ``BackendInstall`` carries the confirmed
        // version. Skipped when the initial probe already matched
        // and no reinstall happened.
        val finalVersion =
            if (needsReinstall) probeCliVersion(venvPython) else venvActualVersion

        return BackendInstall(
            python = venvPython,
            env = buildMap {
                put("HF_HOME", hfHome.toString())
                putAll(ideProxyEnv())
            },
            actualCliVersion = finalVersion,
            expectedCliVersion = expected,
            source = BackendSource.MANAGED_VENV,
        )
    }

    /** Return ``ember_code.__version__`` as reported by the given
     *  Python interpreter, or ``null`` on any failure (import
     *  error, subprocess timeout, missing package, sandboxing).
     *  Kept quick — 2s timeout is well above the observed 30-80 ms
     *  for a cold import + print, and short enough that a broken
     *  interpreter can't block the whole bootstrap. */
    internal fun probeCliVersion(python: Path): String? {
        if (!Files.isExecutable(python)) return null
        return try {
            val proc = ProcessBuilder(
                python.toString(),
                "-c",
                "import ember_code, sys; sys.stdout.write(ember_code.__version__)",
            )
                .redirectErrorStream(true)
                .start()
            val out = proc.inputStream.bufferedReader().use { it.readText() }.trim()
            if (!proc.waitFor(2, java.util.concurrent.TimeUnit.SECONDS)) {
                proc.destroyForcibly()
                return null
            }
            if (proc.exitValue() != 0) return null
            out.takeIf { it.isNotBlank() }
        } catch (e: Exception) {
            log.info("probeCliVersion($python) failed: $e")
            null
        }
    }

    /**
     * Pick up the IDE's configured HTTP proxy (Settings → Appearance
     * & Behavior → System Settings → HTTP Proxy) and translate it
     * into ``HTTPS_PROXY`` / ``HTTP_PROXY`` / ``NO_PROXY`` env vars
     * that ``uv``, ``pip``, and ``urllib`` (which the BE uses) all
     * honor. Without this, a corporate user whose IDE knows about a
     * proxy but whose shell doesn't would see uv / pip / sentence-
     * transformers all fail with cryptic ``Connection refused``.
     *
     * Returns an empty map when no proxy is configured.
     */
    private fun ideProxyEnv(): Map<String, String> {
        return try {
            val http = com.intellij.util.net.HttpConfigurable.getInstance()
            if (!http.USE_HTTP_PROXY && !http.USE_PROXY_PAC) return emptyMap()
            val host = http.PROXY_HOST.orEmpty().trim()
            if (host.isEmpty()) return emptyMap()
            val port = http.PROXY_PORT
            val scheme = if (http.PROXY_TYPE_IS_SOCKS) "socks5" else "http"
            val auth =
                if (http.PROXY_AUTHENTICATION && !http.proxyLogin.isNullOrBlank()) {
                    "${http.proxyLogin}:${http.plainProxyPassword}@"
                } else {
                    ""
                }
            val url = "$scheme://$auth$host:$port"
            val noProxy = http.PROXY_EXCEPTIONS.orEmpty().trim()
            buildMap {
                put("HTTPS_PROXY", url)
                put("HTTP_PROXY", url)
                if (noProxy.isNotEmpty()) put("NO_PROXY", noProxy)
            }
        } catch (e: Exception) {
            log.info("IDE proxy lookup failed; subprocess will use system defaults: $e")
            emptyMap()
        }
    }

    // ── Platform + paths ──

    /** ``~/.cache/ember-code`` on macOS/Linux, ``%LOCALAPPDATA%/ember-code``
     *  on Windows. Picks a stable per-user location that survives
     *  plugin reinstalls. ``internal`` so the unit-test suite can
     *  verify path resolution without spinning up an IDE. */
    internal fun cacheRoot(): Path {
        val os = System.getProperty("os.name").lowercase()
        val home = System.getProperty("user.home")
        return when {
            os.contains("win") -> {
                val local = System.getenv("LOCALAPPDATA") ?: "$home\\AppData\\Local"
                Path.of(local, "ember-code")
            }
            os.contains("mac") -> Path.of(home, "Library", "Caches", "ember-code")
            else -> {
                val xdg = System.getenv("XDG_CACHE_HOME")
                if (xdg.isNullOrBlank()) Path.of(home, ".cache", "ember-code")
                else Path.of(xdg, "ember-code")
            }
        }
    }

    internal fun uvBinName(): String =
        if (System.getProperty("os.name").lowercase().contains("win")) "uv.exe" else "uv"

    internal fun venvPythonRelPath(): String =
        if (System.getProperty("os.name").lowercase().contains("win")) "Scripts/python.exe"
        else "bin/python"

    /** Map the running JVM's OS+arch onto the GitHub release asset
     *  name uv ships. We deliberately fail loud for anything off the
     *  beaten path so silent fallbacks don't leave a half-bootstrapped
     *  cache lying around. ``internal`` for unit-test coverage. */
    internal fun uvTarget(): String {
        val os = System.getProperty("os.name").lowercase()
        val arch = System.getProperty("os.arch").lowercase()
        return when {
            os.contains("mac") && (arch == "aarch64" || arch == "arm64") ->
                "aarch64-apple-darwin"
            os.contains("mac") && (arch == "x86_64" || arch == "amd64") ->
                "x86_64-apple-darwin"
            os.contains("win") -> "x86_64-pc-windows-msvc"
            os.contains("nix") || os.contains("nux") -> when (arch) {
                "aarch64", "arm64" -> "aarch64-unknown-linux-gnu"
                else -> "x86_64-unknown-linux-gnu"
            }
            else -> error("Unsupported platform: os=$os arch=$arch")
        }
    }

    // ── Downloads ──

    private fun downloadUv(target: Path) {
        val ext = if (System.getProperty("os.name").lowercase().contains("win")) "zip" else "tar.gz"
        val triple = uvTarget()
        val url = "https://github.com/astral-sh/uv/releases/download/" +
            "$UV_VERSION/uv-$triple.$ext"
        log.info("Downloading uv from $url")

        val tmp = Files.createTempFile("uv-download-", ".$ext")
        try {
            val client = HttpClient.newBuilder()
                .followRedirects(HttpClient.Redirect.ALWAYS)
                .connectTimeout(Duration.ofSeconds(15))
                .build()
            val req = HttpRequest.newBuilder(URI.create(url))
                .timeout(Duration.ofMinutes(2))
                .GET()
                .build()
            val rsp = client.send(req, HttpResponse.BodyHandlers.ofFile(tmp))
            if (rsp.statusCode() !in 200..299) {
                error("uv download failed: HTTP ${rsp.statusCode()} from $url")
            }
            extractUv(tmp, target, ext)
        } finally {
            runCatching { Files.deleteIfExists(tmp) }
        }
    }

    private fun extractUv(archive: Path, dest: Path, ext: String) {
        Files.createDirectories(dest.parent)
        val parentDir = dest.parent
        if (ext == "zip") {
            // The Windows zip contains uv.exe at the top level.
            java.util.zip.ZipInputStream(Files.newInputStream(archive)).use { zin ->
                var entry = zin.nextEntry
                while (entry != null) {
                    if (!entry.isDirectory && entry.name.endsWith("uv.exe")) {
                        Files.copy(zin, dest, StandardCopyOption.REPLACE_EXISTING)
                        return
                    }
                    entry = zin.nextEntry
                }
            }
            error("uv binary not found in archive $archive")
        } else {
            // tar.gz on Unix. Use the system tar — JDK has no built-in.
            // Extract into the parent of ``dest`` and look for the
            // single ``uv`` file inside ``uv-<triple>/uv``.
            val extractDir = Files.createTempDirectory(parentDir, "uv-extract-")
            try {
                val proc = ProcessBuilder("tar", "xzf", archive.toString(), "-C", extractDir.toString())
                    .redirectErrorStream(true)
                    .start()
                if (!proc.waitFor(60, java.util.concurrent.TimeUnit.SECONDS)) {
                    proc.destroyForcibly()
                    error("tar extraction of uv timed out")
                }
                if (proc.exitValue() != 0) {
                    error("tar extraction of uv failed (exit ${proc.exitValue()})")
                }
                // Find uv inside the extracted tree.
                val found = Files.walk(extractDir).use { stream ->
                    stream.filter { it.fileName.toString() == "uv" && Files.isRegularFile(it) }
                        .findFirst().orElse(null)
                } ?: error("uv binary missing from extracted archive")
                Files.move(found, dest, StandardCopyOption.REPLACE_EXISTING)
                dest.toFile().setExecutable(true, false)
            } finally {
                runCatching { deleteRecursively(extractDir) }
            }
        }
    }

    // ── Subprocess invocation ──

    private fun runUv(uv: Path, args: List<String>) {
        runProcess(uv, args)
    }

    /** Run a subprocess with our managed environment layer. Used for
     *  ``uv`` invocations and for the post-install model prefetch,
     *  both of which need the same log-and-fail-loud behaviour. */
    private fun runProcess(
        bin: Path,
        args: List<String>,
        env: Map<String, String> = emptyMap(),
    ) {
        val cmd = listOf(bin.toString()) + args
        log.info("Running: ${cmd.joinToString(" ")}")
        val builder = ProcessBuilder(cmd).redirectErrorStream(true)
        if (env.isNotEmpty()) builder.environment().putAll(env)
        val proc = builder.start()
        val output = StringBuilder()
        Thread {
            proc.inputStream.bufferedReader().use { reader ->
                reader.forEachLine { line ->
                    synchronized(output) {
                        output.append(line).append('\n')
                        if (output.length > 8192) output.delete(0, output.length - 8192)
                    }
                    log.debug("subprocess: $line")
                }
            }
        }.apply { isDaemon = true; start() }

        if (!proc.waitFor(10, java.util.concurrent.TimeUnit.MINUTES)) {
            proc.destroyForcibly()
            error("Command timed out: ${bin.fileName} ${args.joinToString(" ")}")
        }
        if (proc.exitValue() != 0) {
            val tail = synchronized(output) { output.toString().trim() }
            error("Command failed (exit ${proc.exitValue()}): ${bin.fileName} ${args.joinToString(" ")}\n$tail")
        }
    }

    // ── Utilities ──

    /** Throw with a clear message if the filesystem holding
     *  ``cache`` doesn't have at least ``minBytes`` free. ``internal``
     *  for unit-test coverage. */
    internal fun ensureFreeSpace(cache: Path, minBytes: Long, listener: (String) -> Unit) {
        // ``cache`` may not exist yet — walk up until we find an
        // existing ancestor so ``getFreeSpace`` returns a real number.
        var probe: Path? = cache
        while (probe != null && !Files.exists(probe)) probe = probe.parent
        val free = probe?.toFile()?.freeSpace ?: return
        if (free >= minBytes) return
        val freeMb = free / (1024 * 1024)
        val needMb = minBytes / (1024 * 1024)
        val msg = "Not enough disk space for the Ember backend bootstrap: " +
            "${freeMb} MB free at ${cache}, need at least ${needMb} MB. " +
            "Free up space and try again."
        listener("Disk space check failed.")
        error(msg)
    }

    private fun deleteRecursively(path: Path) {
        if (!Files.exists(path)) return
        Files.walk(path).use { stream ->
            stream.sorted(Comparator.reverseOrder())
                .forEach { p -> runCatching { Files.delete(p) } }
        }
    }

    /** Wipe the entire managed cache and force a re-bootstrap on the
     *  next ``ensureBackendPython`` call. Used by the "Restart
     *  backend (clean install)" action so users can recover from a
     *  corrupted venv without leaving the IDE. */
    fun resetCache() {
        val root = cacheRoot()
        if (Files.exists(root)) {
            log.info("Wiping Ember managed cache: $root")
            deleteRecursively(root)
        }
    }
}

package sh.igniteember.embercode

import com.intellij.notification.NotificationGroupManager
import com.intellij.notification.NotificationType
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.components.service
import com.intellij.openapi.fileEditor.FileEditorManager
import com.intellij.openapi.fileEditor.OpenFileDescriptor
import com.intellij.openapi.project.Project
import com.intellij.openapi.vfs.LocalFileSystem
import com.intellij.openapi.wm.ToolWindow
import com.intellij.openapi.wm.ToolWindowFactory
import com.intellij.ui.jcef.JBCefBrowser
import com.intellij.ui.jcef.JBCefBrowserBase
import com.intellij.ui.jcef.JBCefJSQuery
import com.intellij.ui.components.JBLabel
import com.intellij.util.ui.JBUI
import com.sun.net.httpserver.HttpExchange
import com.sun.net.httpserver.HttpServer
import org.cef.browser.CefBrowser
import org.cef.browser.CefFrame
import org.cef.handler.CefLoadHandlerAdapter
import java.io.File
import java.net.InetSocketAddress
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.StandardCopyOption
import javax.swing.JPanel
import java.awt.BorderLayout

/**
 * "igni" tool window — a JCEF browser hosting the shared web UI
 * (clients/web, bundled under /webui in plugin resources), connected
 * to the project's backend via `?ws=` query param.
 */
class EmberToolWindowFactory : ToolWindowFactory {

    override fun createToolWindowContent(project: Project, toolWindow: ToolWindow) {
        val panel = JPanel(BorderLayout())
        val statusLabel = JBLabel("Preparing Ember backend…", JBLabel.CENTER).apply {
            border = JBUI.Borders.empty(24)
        }
        panel.add(statusLabel, BorderLayout.CENTER)
        val content = toolWindow.contentManager.factory.createContent(panel, "", false)
        toolWindow.contentManager.addContent(content)

        // JCEF is required for the chat panel. Most JetBrains
        // distributions ship it via the bundled JBR, but some
        // alternate JRE / corporate-locked configurations don't.
        // Surface a clean error early so we don't crash trying to
        // construct ``JBCefBrowser`` against a missing native lib.
        if (!com.intellij.ui.jcef.JBCefApp.isSupported()) {
            statusLabel.text = "<html>JCEF is not available in this IDE / JRE.<br>" +
                "Switch to the bundled JBR (Help → Find Action → Choose Boot Java Runtime) " +
                "and reopen the project.</html>"
            return
        }

        val backend = project.service<EmberBackendService>()
        // Bootstrap progress — uv download / Python install /
        // ignite-ember pip install can take a few minutes on first
        // launch. Pipe ``EmberRuntime`` status strings into the
        // loading label so the user sees what's happening instead of
        // staring at a stale "Preparing…" line.
        backend.progressListener = { msg ->
            ApplicationManager.getApplication().invokeLater { statusLabel.text = msg }
        }
        backend.ensureStarted().whenComplete { port, err ->
            ApplicationManager.getApplication().invokeLater {
                backend.progressListener = null
                panel.removeAll()
                if (err != null || port == null) {
                    panel.add(
                        JBLabel(
                            "<html>Ember backend failed to start.<br>" +
                                "${err?.message ?: "Unknown error"}<br><br>" +
                                "Try <b>Tools → igni → Reinstall Backend</b>.</html>",
                            JBLabel.CENTER,
                        ),
                        BorderLayout.CENTER,
                    )
                } else {
                    val baseUrl = serveWebUi()
                    // Propagate the resolved / expected ``ignite-ember``
                    // versions into the page URL so the web UI can
                    // render a tiny "cli · vX.Y.Z" chip in the header
                    // — the reader can *see* what's actually running
                    // without spelunking through logs. When actual
                    // and expected disagree the chip goes red;
                    // clicking it can then invoke Tools → igni →
                    // Diagnose Backend for the full report.
                    val install = backend.lastInstall
                    val versionQuery = if (install != null) {
                        val actual = install.actualCliVersion ?: "unknown"
                        val expected = install.expectedCliVersion
                        val source = install.source.name.lowercase()
                        "&host=jetbrains" +
                            "&expected_cli=${java.net.URLEncoder.encode(expected, "UTF-8")}" +
                            "&actual_cli=${java.net.URLEncoder.encode(actual, "UTF-8")}" +
                            "&backend_source=$source"
                    } else {
                        "&host=jetbrains"
                    }
                    // Windowed mode (``setOffScreenRendering(false)``)
                    // uses a heavyweight AWT canvas that hosts a real
                    // native browser window, and paints at the OS
                    // display rate. OSR (the default) pipes frames
                    // through Chromium's ``windowless_frame_rate`` (30
                    // by default) then hands them to the AWT paint
                    // loop, which itself schedules at ~30 Hz on macOS
                    // — the ``setWindowlessFrameRate`` runtime setter
                    // changes the Chromium clock but the JBR paint
                    // side stays at 30 Hz. Windowed mode bypasses
                    // both. Trade-off: no transparent overlays over
                    // the browser (fine — our tool window doesn't
                    // paint anything above it) and slightly worse
                    // integration with the IDE's own animation layer.
                    val browser = com.intellij.ui.jcef.JBCefBrowserBuilder()
                        .setOffScreenRendering(false)
                        .setUrl("$baseUrl/index.html?ws=ws%3A%2F%2F127.0.0.1%3A$port$versionQuery")
                        .build()
                    installHostBridge(project, browser)
                    BROWSERS[project] = browser
                    installThemeBridge(project, browser)
                    com.intellij.openapi.util.Disposer.register(browser) {
                        BROWSERS.remove(project, browser)
                    }
                    panel.add(browser.component, BorderLayout.CENTER)
                }
                panel.revalidate()
                panel.repaint()
            }
        }
    }

    /**
     * Serve the bundled web UI over a tiny loopback HTTP server.
     *
     * Why HTTP instead of ``file://``? The shared web UI is shipped
     * as ES modules (Vite's default ``<script type="module">``).
     * Chromium refuses to load module scripts from a ``file://``
     * origin under CORS — the HTML loads, the JS never runs, the
     * panel paints blank. Serving from ``http://127.0.0.1:N`` gives
     * the bundle a real origin and modules load normally. JCEF treats
     * a loopback HTTP origin like any other web origin, so our
     * WebSocket connect to the BE just works (same loopback).
     *
     * One-shot per session: we extract resources into a temp dir,
     * spawn an ``HttpServer`` bound to an ephemeral port, return the
     * base URL. The temp dir + server live for the IDE's lifetime —
     * negligible footprint (3 small files + 1 IO thread).
     */
    private fun serveWebUi(): String {
        val dir = Files.createTempDirectory("ember-webui")
        val cl = javaClass.classLoader

        // Vite emits index.html + hashed assets. Extract them so the
        // HTTP server can stream from a known directory rather than
        // touching the plugin jar on every request.
        cl.getResourceAsStream("webui/index.html")?.use { input ->
            Files.copy(input, dir.resolve("index.html"), StandardCopyOption.REPLACE_EXISTING)
        }
        val index = dir.resolve("index.html")
        if (Files.exists(index)) {
            val html = Files.readString(index)
            Regex("\\./(assets/[A-Za-z0-9._-]+)").findAll(html).forEach { m ->
                val rel = m.groupValues[1]
                cl.getResourceAsStream("webui/$rel")?.use { input ->
                    val target = dir.resolve(rel)
                    Files.createDirectories(target.parent)
                    Files.copy(input, target, StandardCopyOption.REPLACE_EXISTING)
                }
            }
        }

        // Bind to 127.0.0.1:0 — loopback only, ephemeral port chosen
        // by the OS. We never expose this externally; only JCEF reaches
        // it. Daemon executor lets the IDE shut down cleanly.
        val server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        server.createContext("/") { exchange ->
            handleHttpRequest(exchange, dir)
        }
        server.executor = java.util.concurrent.Executors.newSingleThreadExecutor { r ->
            Thread(r, "ember-webui-http").apply { isDaemon = true }
        }
        server.start()
        val port = server.address.port
        return "http://127.0.0.1:$port"
    }

    private fun handleHttpRequest(exchange: HttpExchange, root: Path) {
        try {
            // Strip leading slash and query string. We never serve
            // outside the served dir — strip "../" segments defensively.
            val rawPath = exchange.requestURI.path.trimStart('/')
            val safePath = rawPath.ifEmpty { "index.html" }
                .replace("..", "")
            val target = root.resolve(safePath).normalize()
            if (!target.startsWith(root) || !Files.isRegularFile(target)) {
                exchange.sendResponseHeaders(404, -1)
                exchange.close()
                return
            }
            val bytes = Files.readAllBytes(target)
            val mime = when (target.toString().substringAfterLast('.').lowercase()) {
                "html" -> "text/html; charset=utf-8"
                "js" -> "application/javascript; charset=utf-8"
                "css" -> "text/css; charset=utf-8"
                "svg" -> "image/svg+xml"
                "json" -> "application/json"
                else -> "application/octet-stream"
            }
            exchange.responseHeaders.add("Content-Type", mime)
            exchange.sendResponseHeaders(200, bytes.size.toLong())
            exchange.responseBody.use { it.write(bytes) }
        } catch (e: Exception) {
            try {
                exchange.sendResponseHeaders(500, -1)
            } catch (_: Exception) { /* already responded */ }
        } finally {
            try { exchange.close() } catch (_: Exception) {}
        }
    }

    /**
     * Wire JCEF's ``cefQuery`` so the shared web UI can call native IDE
     * actions: open a file in the editor, post an IDE notification, …
     *
     * The web client (clients/web/src/lib/host.ts) detects ``cefQuery``
     * and emits JSON-encoded requests of shape::
     *
     *     {"type": "ember:openFile", "path": "/abs/path/to/file"}
     *     {"type": "ember:notify",   "title": "…", "body": "…"}
     *
     * We register one JBCefJSQuery instance and inject a tiny JS shim
     * that exposes ``window.cefQuery({request, onSuccess, onFailure})``
     * on every page load, so the FE's detection ("typeof
     * window.cefQuery === 'function'") succeeds.
     */
    private fun installHostBridge(project: Project, browser: JBCefBrowser) {
        val query = JBCefJSQuery.create(browser as JBCefBrowserBase)

        query.addHandler { raw ->
            // Route by type. Fire-and-forget requests (openFile,
            // notify) defer to the EDT and return null — JCEF won't
            // call the FE's onSuccess. Request/response queries
            // (searchCode) compute synchronously and return a
            // JBCefJSQuery.Response carrying the JSON payload.
            when (parseJsonField(raw, "type")) {
                "ember:searchCode" -> {
                    try {
                        JBCefJSQuery.Response(handleSearchCode(project, raw))
                    } catch (e: Exception) {
                        // Surfaced as onFailure on the FE; the composer
                        // falls back to the WS RPC.
                        JBCefJSQuery.Response(null, 1, e.message ?: "search failed")
                    }
                }
                else -> {
                    ApplicationManager.getApplication().invokeLater {
                        handleHostRequest(project, raw)
                    }
                    null
                }
            }
        }

        // Inject the shim every time a frame finishes loading so the FE
        // sees ``window.cefQuery`` as soon as it runs ``detectHost``.
        // ``query.inject(request, onSuccess, onFailure)`` returns the
        // JS expression that performs the round-trip; we forward the
        // caller's optional callbacks so request/response queries
        // (``host.searchCode``) can resolve their Promises.
        val shim = """
            window.cefQuery = function(opts) {
                if (!opts || typeof opts.request !== 'string') return;
                ${query.inject(
                    "opts.request",
                    "function(r){ if (typeof opts.onSuccess === 'function') opts.onSuccess(r); }",
                    "function(c, m){ if (typeof opts.onFailure === 'function') opts.onFailure(c, m); }",
                )};
            };
        """.trimIndent()

        // Inject as early as possible. ``onLoadStart`` fires before
        // the document begins parsing — the FE's ``detectHost()``
        // (which runs during initial module load) then sees
        // ``window.cefQuery`` and classifies the host as ``jetbrains``.
        // Without this the first detect returned ``"web"``, the
        // project chip stayed visible, and IDE-only UI didn't activate
        // until something forced a re-detect. We ALSO re-inject on
        // ``onLoadEnd`` as belt-and-suspenders for navigations / hot
        // reloads.
        browser.jbCefClient.addLoadHandler(object : CefLoadHandlerAdapter() {
            override fun onLoadStart(
                cefBrowser: CefBrowser?,
                frame: CefFrame?,
                transitionType: org.cef.network.CefRequest.TransitionType?,
            ) {
                cefBrowser?.executeJavaScript(shim, cefBrowser.url, 0)
            }

            override fun onLoadEnd(cefBrowser: CefBrowser?, frame: CefFrame?, httpStatusCode: Int) {
                cefBrowser?.executeJavaScript(shim, cefBrowser.url, 0)
            }
        }, browser.cefBrowser)
    }

    private fun handleHostRequest(project: Project, raw: String) {
        val type = parseJsonField(raw, "type") ?: return
        when (type) {
            "ember:openFile" -> {
                val rawPath = parseJsonField(raw, "path") ?: return
                // Format: ``<path>:<start>[-<end>]`` — start/end optional
                // line numbers. ``<path>`` may be absolute or
                // project-relative (search_code returns relative paths).
                // Guard the line capture with a digit anchor so Windows
                // absolute paths ("C:\\…") aren't accidentally truncated.
                val lineMatch = Regex("^(.*?):(\\d+)(?:-(\\d+))?$").matchEntire(rawPath)
                val filePart = lineMatch?.groupValues?.get(1) ?: rawPath
                val startLine = lineMatch?.groupValues?.get(2)?.toIntOrNull()
                val endLine = lineMatch?.groupValues?.get(3)
                    ?.takeIf { it.isNotEmpty() }?.toIntOrNull()

                // Resolve relative paths against the project root.
                // search_code returns project-relative paths; the
                // composer's @<path> mentions are usually absolute but
                // we accept either shape transparently.
                val absPath = if (File(filePart).isAbsolute) {
                    filePart
                } else {
                    val base = project.basePath ?: return
                    "$base/$filePart"
                }

                // Fast path: hit the VFS without forcing a refresh.
                // Fall back to refresh only if the file isn't already
                // known (rare — happens just after files appear on
                // disk and the IDE hasn't noticed yet).
                val lfs = LocalFileSystem.getInstance()
                val vf = lfs.findFileByPath(absPath)
                    ?: lfs.refreshAndFindFileByPath(absPath)
                    ?: return

                if (startLine == null) {
                    FileEditorManager.getInstance(project).openFile(vf, true)
                    return
                }

                // Open at the start line; then, if a range was given,
                // select [startLine, endLine] so the user can SEE the
                // snippet they pasted, not just the first line.
                OpenFileDescriptor(project, vf, (startLine - 1).coerceAtLeast(0), 0)
                    .navigate(true)
                if (endLine != null && endLine > startLine) {
                    val editor = FileEditorManager.getInstance(project).selectedTextEditor
                    if (editor != null && editor.virtualFile == vf) {
                        val doc = editor.document
                        val maxLine = (doc.lineCount - 1).coerceAtLeast(0)
                        val s = (startLine - 1).coerceIn(0, maxLine)
                        val e = (endLine - 1).coerceIn(0, maxLine)
                        editor.selectionModel.setSelection(
                            doc.getLineStartOffset(s),
                            doc.getLineEndOffset(e),
                        )
                        editor.caretModel.moveToOffset(doc.getLineStartOffset(s))
                    }
                }
            }
            "ember:fileEdited" -> {
                val rawPath = parseJsonField(raw, "path") ?: return
                // BE always emits absolute paths (it resolves via
                // ``_resolve_path`` before writing). Tolerate
                // relative paths anyway in case a future caller
                // sends them; resolve against the project root.
                val absPath = if (File(rawPath).isAbsolute) {
                    rawPath
                } else {
                    val base = project.basePath ?: return
                    "$base/$rawPath"
                }
                // ``refreshAndFindFileByPath`` walks back up to the
                // closest cached VFS ancestor and refreshes only the
                // changed file — much cheaper than refreshing the
                // whole project. The VFS refresh hits
                // ``LocalHistoryImpl``'s VFS listener which
                // automatically snapshots the previous contents.
                // Open editors reload their text from disk.
                com.intellij.openapi.application.WriteAction.runAndWait<RuntimeException> {
                    LocalFileSystem.getInstance().refreshAndFindFileByPath(absPath)
                }
            }
            "ember:notify" -> {
                val title = parseJsonField(raw, "title") ?: "Ember"
                val body = parseJsonField(raw, "body") ?: ""
                NotificationGroupManager.getInstance()
                    .getNotificationGroup("EmberCode")
                    .createNotification(title, body, NotificationType.INFORMATION)
                    .notify(project)
            }
        }
    }

    /**
     * Pull a top-level string field out of a JSON object without bringing
     * in a JSON library. The shared web UI only ever sends flat
     * ``{"type": "...", "path"/"title"/"body": "..."}`` objects, so a
     * permissive regex is enough — and keeps this plugin dep-free.
     */
    private fun parseJsonField(json: String, key: String): String? {
        val re = Regex("\"${Regex.escape(key)}\"\\s*:\\s*\"((?:\\\\.|[^\"\\\\])*)\"")
        val m = re.find(json) ?: return null
        // The FE emits two escape sequences (``\"`` and ``\\``) plus
        // ``\n`` for newlines inside multi-line snippets — search_code's
        // snippet field passes whole code blocks across the bridge.
        return m.groupValues[1]
            .replace("\\\"", "\"")
            .replace("\\n", "\n")
            .replace("\\r", "\r")
            .replace("\\t", "\t")
            .replace("\\\\", "\\")
    }

    /**
     * Indexed substring search across the project — the JetBrains-native
     * answer to the WS ``search_code`` RPC. Uses PyCharm's trigram /
     * word index (the same one Find-in-Files relies on) to narrow the
     * candidate set, then confirms each candidate against the full
     * multi-line snippet.
     *
     * Returns a JSON payload shaped EXACTLY like the WS
     * ``search_code`` response (``{matches: [{path, line, end_line,
     * preview}], truncated}``) so the FE consumer doesn't care which
     * code path supplied it — see ``host.searchCode`` in
     * ``clients/web/src/lib/host.ts``.
     */
    private fun handleSearchCode(project: Project, raw: String): String {
        val snippet = parseJsonField(raw, "snippet") ?: return EMPTY_SEARCH_JSON
        if (snippet.length < 5) return EMPTY_SEARCH_JSON

        // While the project is indexing (right after first open, or
        // after a large refresh), the trigram lookup returns nothing.
        // Bail out so the FE falls back to the WS rg path — the user
        // gets results, just via the slower lane, instead of a
        // silent "no matches" that's actually "index not ready yet".
        if (com.intellij.openapi.project.DumbService.isDumb(project)) return EMPTY_SEARCH_JSON

        val results = com.intellij.openapi.application.ReadAction
            .compute<List<SearchMatch>, RuntimeException> {
                indexedSearch(project, snippet, maxResults = 20)
            }
        return matchesToJson(results)
    }

    /** One row of the search_code response. */
    private data class SearchMatch(
        val path: String,
        val line: Int,
        val endLine: Int,
        val preview: String,
    )

    private fun indexedSearch(
        project: Project,
        snippet: String,
        maxResults: Int,
    ): List<SearchMatch> {
        val basePath = project.basePath ?: ""
        val snippetLines = snippet.count { it == '\n' } + 1
        val firstNonBlank = snippet.lineSequence()
            .firstOrNull { it.isNotBlank() }
            ?.trim()
            ?: snippet
        // Trigram bucket key. ~50 chars is plenty to narrow the candidate
        // set; longer than that and the bucket-lookup itself becomes
        // slower than just confirming a few extra files.
        val indexKey = firstNonBlank.take(50)
        if (indexKey.length < 3) return emptyList() // index needs ≥3 chars

        val results = mutableListOf<SearchMatch>()
        val helper = com.intellij.psi.search.PsiSearchHelper.getInstance(project)
        val scope = com.intellij.psi.search.GlobalSearchScope.projectScope(project)

        // ``ANY`` (union of IN_CODE | IN_COMMENTS | IN_STRINGS |
        // IN_FOREIGN_LANGUAGES | IN_PLAIN_TEXT) is what Find-in-Files
        // uses by default. ``IN_PLAIN_TEXT`` alone only matches .txt /
        // .md / similar — tokens inside .py / .ts / .kt source bucket
        // under IN_CODE, IN_STRINGS, and IN_COMMENTS, so a Python
        // identifier like ``_find_free_port`` was invisible to a
        // PLAIN_TEXT-only query and the search returned zero candidates.
        helper.processCandidateFilesForText(
            scope,
            com.intellij.psi.search.UsageSearchContext.ANY,
            true,
            indexKey,
        ) { file: com.intellij.openapi.vfs.VirtualFile ->
            if (results.size >= maxResults) return@processCandidateFilesForText false
            if (file.length > 2L * 1024 * 1024) return@processCandidateFilesForText true
            if (file.fileType.isBinary) return@processCandidateFilesForText true

            try {
                val text = String(file.contentsToByteArray(), file.charset)
                val idx = text.indexOf(snippet)
                if (idx >= 0) {
                    val line = text.substring(0, idx).count { it == '\n' } + 1
                    val rel = if (basePath.isNotEmpty() && file.path.startsWith("$basePath/")) {
                        file.path.removePrefix("$basePath/")
                    } else {
                        file.path
                    }
                    results.add(
                        SearchMatch(
                            path = rel,
                            line = line,
                            endLine = line + snippetLines - 1,
                            preview = firstNonBlank,
                        ),
                    )
                }
            } catch (_: Exception) {
                // Unreadable / binary that slipped past the check — skip.
            }
            true
        }
        return results
    }

    private fun matchesToJson(matches: List<SearchMatch>): String {
        val sb = StringBuilder("""{"matches":[""")
        matches.forEachIndexed { i, m ->
            if (i > 0) sb.append(',')
            sb.append("{\"path\":\"").append(jsonEscape(m.path)).append("\",")
            sb.append("\"line\":").append(m.line).append(',')
            sb.append("\"end_line\":").append(m.endLine).append(',')
            sb.append("\"preview\":\"").append(jsonEscape(m.preview)).append("\"}")
        }
        sb.append("""],"truncated":false}""")
        return sb.toString()
    }

    private fun jsonEscape(s: String): String {
        val sb = StringBuilder(s.length + 8)
        for (c in s) {
            when (c) {
                '\\' -> sb.append("\\\\")
                '"' -> sb.append("\\\"")
                '\n' -> sb.append("\\n")
                '\r' -> sb.append("\\r")
                '\t' -> sb.append("\\t")
                else -> {
                    if (c.code < 0x20) {
                        sb.append("\\u").append("%04x".format(c.code))
                    } else {
                        sb.append(c)
                    }
                }
            }
        }
        return sb.toString()
    }

    /**
     * Subscribe to IntelliJ's LAF changes and push the current theme
     * polarity (dark / light) into the web UI. The web client looks
     * for ``data-theme`` on ``<html>`` (see ``clients/web/src/theme.css``);
     * setting it from the IDE overrides the OS ``prefers-color-scheme``
     * default so PyCharm in Light theme + macOS in Dark mode no longer
     * leaves the chat panel mismatched.
     *
     * Initial push fires after the page's first load so the right
     * theme is applied before the first paint of the chat surface.
     * Subsequent pushes happen on every LAF change.
     */
    private fun installThemeBridge(project: Project, browser: JBCefBrowser) {
        // Push the initial value on every page load so SPA refreshes
        // (and the JCEF initial paint) pick up the right theme. We
        // can't rely on ``onLoadEnd`` because the host shim is also
        // injected there and ordering matters; instead we re-push on
        // any frame load.
        browser.jbCefClient.addLoadHandler(object : CefLoadHandlerAdapter() {
            override fun onLoadEnd(cefBrowser: CefBrowser?, frame: CefFrame?, httpStatusCode: Int) {
                pushTheme(project)
            }
        }, browser.cefBrowser)

        // Live updates while the browser stays open. The connection
        // is tied to the browser's Disposable so we don't leak past
        // tool-window close.
        val bus = com.intellij.openapi.application.ApplicationManager.getApplication()
            .messageBus.connect(browser)
        bus.subscribe(
            com.intellij.ide.ui.LafManagerListener.TOPIC,
            com.intellij.ide.ui.LafManagerListener { pushTheme(project) },
        )
    }

    /** Read the current LAF's background luminance and push
     *  ``data-theme=dark|light`` PLUS the raw ``Panel.background``
     *  color to the web client. The polarity flag drives the
     *  bulk of the palette (dark vs light branches in
     *  ``theme.css``) while the raw bg gets applied as a CSS
     *  custom property so the tool window's own background
     *  matches the surrounding IDE chrome exactly — no more
     *  "patch of the wrong colour" against Darcula / High
     *  Contrast / any custom theme. Luminance threshold at
     *  ~50% is robust across the dozen+ shipped themes without
     *  needing to enumerate them by name. */
    private fun pushTheme(project: Project) {
        val bg = javax.swing.UIManager.getColor("Panel.background") ?: return
        val luma = (bg.red * 0.299 + bg.green * 0.587 + bg.blue * 0.114)
        val dark = luma < 128
        val hex = String.format("#%02x%02x%02x", bg.red, bg.green, bg.blue)
        pushEvent(
            project,
            "ember:theme",
            """{"dark":$dark,"bg":"$hex"}""",
        )
    }

    companion object {
        private const val EMPTY_SEARCH_JSON = """{"matches":[],"truncated":false}"""

        /** Per-project active JCEF browser, used to dispatch
         *  IDE→FE events (selection forwarded from the editor, file
         *  attachments from the project view, …). Set when the
         *  tool window mounts; cleared via the browser's Disposable. */
        private val BROWSERS =
            java.util.concurrent.ConcurrentHashMap<Project, JBCefBrowser>()

        /** Surface an arbitrary IDE-side event into the web UI. The
         *  FE listens on ``window.addEventListener('ember-host',
         *  …)`` — see ``clients/web/src/lib/host.ts``. Encodes the
         *  payload as a CustomEvent so handlers don't have to parse
         *  out the type. */
        fun pushEvent(project: Project, type: String, jsonPayload: String) {
            val browser = BROWSERS[project] ?: return
            val cef = browser.cefBrowser ?: return
            // ``jsonPayload`` is already JSON; embed it raw so the FE
            // can read structured fields. ``type`` is alphanumeric so
            // no escaping needed.
            val script = """
                (function(){
                  var ev = new CustomEvent('ember-host', {
                    detail: { type: '$type', payload: $jsonPayload }
                  });
                  window.dispatchEvent(ev);
                })();
            """.trimIndent()
            cef.executeJavaScript(script, cef.url, 0)
        }

        /** Toggle the FPS counter overlay in the web UI. The web
         *  bundle installs ``window.__igni_toggleFps`` at mount;
         *  the FPS overlay's own ``Cmd+Alt+Shift+F`` keyboard
         *  path doesn't work in JCEF because IntelliJ swallows
         *  the keystroke before it reaches the DOM — so we
         *  route the toggle through an IDE ``AnAction`` that
         *  calls this function. */
        fun toggleFpsOverlay(project: Project) {
            val browser = BROWSERS[project] ?: return
            val cef = browser.cefBrowser ?: return
            val script =
                "if (typeof window.__igni_toggleFps === 'function') " +
                    "window.__igni_toggleFps();"
            cef.executeJavaScript(script, cef.url, 0)
        }
    }
}

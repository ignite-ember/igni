package sh.igniteember.embercode.actions

import com.intellij.openapi.project.Project
import sh.igniteember.embercode.EmberToolWindowFactory

/**
 * Helpers to surface IDE → web-UI events. The tool-window factory
 * holds a per-project list of subscribers (the JCEF browser bridge);
 * actions just call into these helpers without needing direct access
 * to the JBCefBrowser.
 *
 * Why a separate file? Actions live under ``actions/``; the bridge
 * itself lives in ``EmberToolWindowFactory``. The cross-package
 * dispatch table sits here so neither side has to know the other's
 * internals beyond the (path, range, text) payload.
 */
object EmberHostEvents {
    /** Send the given selection to the chat composer. The web UI
     *  appends it as a code-paste pill (the same one the paste
     *  handler would produce). */
    fun addToComposer(
        project: Project,
        path: String?,
        text: String,
        startLine: Int? = null,
        endLine: Int? = null,
    ) {
        EmberToolWindowFactory.pushEvent(
            project,
            "ember:addToComposer",
            composerPayload(path, text, startLine, endLine),
        )
    }

    /** Surface a file as an attachment in the chat composer. */
    fun attachFile(project: Project, path: String) {
        EmberToolWindowFactory.pushEvent(
            project,
            "ember:attachFile",
            attachFilePayload(path),
        )
    }

    /** Pure JSON-payload builder for ``ember:addToComposer``. Split
     *  out from ``addToComposer`` so the wire shape is unit-testable
     *  without standing up an IntelliJ Platform Project fixture.
     *
     *  ``path`` is nullable because untitled / unsaved-buffer
     *  selections have no on-disk location; the FE renders the pill
     *  without a file label when it's missing. ``startLine``/
     *  ``endLine`` are 1-indexed (FE convention). */
    internal fun composerPayload(
        path: String?,
        text: String,
        startLine: Int? = null,
        endLine: Int? = null,
    ): String = buildString {
        append('{')
        append("\"text\":\"").append(jsonEscape(text)).append('"')
        if (path != null) {
            append(",\"path\":\"").append(jsonEscape(path)).append('"')
        }
        if (startLine != null) append(",\"line\":").append(startLine)
        if (endLine != null) append(",\"end_line\":").append(endLine)
        append('}')
    }

    /** Pure JSON-payload builder for ``ember:attachFile``. */
    internal fun attachFilePayload(path: String): String =
        "{\"path\":\"${jsonEscape(path)}\"}"

    internal fun jsonEscape(s: String): String {
        val sb = StringBuilder(s.length + 8)
        for (c in s) {
            when (c) {
                '\\' -> sb.append("\\\\")
                '"' -> sb.append("\\\"")
                '\n' -> sb.append("\\n")
                '\r' -> sb.append("\\r")
                '\t' -> sb.append("\\t")
                else -> {
                    if (c.code < 0x20) sb.append("\\u").append("%04x".format(c.code))
                    else sb.append(c)
                }
            }
        }
        return sb.toString()
    }
}

package sh.igniteember.embercode.actions

import com.intellij.openapi.actionSystem.ActionUpdateThread
import com.intellij.openapi.actionSystem.AnAction
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.actionSystem.CommonDataKeys
import com.intellij.openapi.wm.ToolWindowManager

/**
 * "Add Selection to Ember Chat" — visible in the editor context
 * menu and bound to ⌘E / Ctrl+E. Sends the highlighted text plus
 * file path + line range to the chat composer where it appears as
 * a code-paste pill (same UX as pasting from the clipboard).
 *
 * Falls back to the line under the caret when there's no selection,
 * so the shortcut is useful without a manual highlight.
 */
class AddSelectionToChatAction : AnAction() {
    override fun getActionUpdateThread(): ActionUpdateThread = ActionUpdateThread.BGT

    override fun update(e: AnActionEvent) {
        val editor = e.getData(CommonDataKeys.EDITOR)
        e.presentation.isEnabledAndVisible = editor != null && e.project != null
    }

    override fun actionPerformed(e: AnActionEvent) {
        val project = e.project ?: return
        val editor = e.getData(CommonDataKeys.EDITOR) ?: return
        val doc = editor.document
        val sel = editor.selectionModel
        val (text, startLine, endLine) = if (sel.hasSelection()) {
            val s = sel.selectionStart
            val eo = sel.selectionEnd
            Triple(
                doc.getText(com.intellij.openapi.util.TextRange(s, eo)),
                doc.getLineNumber(s) + 1,
                doc.getLineNumber((eo - 1).coerceAtLeast(s)) + 1,
            )
        } else {
            val line = editor.caretModel.logicalPosition.line
            val s = doc.getLineStartOffset(line)
            val eo = doc.getLineEndOffset(line)
            Triple(doc.getText(com.intellij.openapi.util.TextRange(s, eo)), line + 1, line + 1)
        }

        val vf = e.getData(CommonDataKeys.VIRTUAL_FILE)
        val rel = vf?.let { PathUtils.projectRelative(it.path, project.basePath) }

        EmberHostEvents.addToComposer(project, rel, text, startLine, endLine)

        // Surface the chat panel so the user sees the new pill.
        ToolWindowManager.getInstance(project)
            .getToolWindow("igni")?.show(null)
    }
}

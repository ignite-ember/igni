package sh.igniteember.embercode.actions

import com.intellij.openapi.actionSystem.ActionUpdateThread
import com.intellij.openapi.actionSystem.AnAction
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.actionSystem.CommonDataKeys
import com.intellij.openapi.wm.ToolWindowManager

/** Project view → Ember Chat. Attaches each selected non-directory
 *  file as a composer attachment — same shape the ``+`` button in
 *  the composer produces. */
class AddFileToChatAction : AnAction() {
    override fun getActionUpdateThread(): ActionUpdateThread = ActionUpdateThread.BGT

    override fun update(e: AnActionEvent) {
        val files = e.getData(CommonDataKeys.VIRTUAL_FILE_ARRAY)
        e.presentation.isEnabledAndVisible = !files.isNullOrEmpty() && e.project != null
    }

    override fun actionPerformed(e: AnActionEvent) {
        val project = e.project ?: return
        val files = e.getData(CommonDataKeys.VIRTUAL_FILE_ARRAY) ?: return
        val base = project.basePath
        for (vf in files) {
            if (vf.isDirectory) continue
            val rel = PathUtils.projectRelative(vf.path, base)
            EmberHostEvents.attachFile(project, rel)
        }
        ToolWindowManager.getInstance(project)
            .getToolWindow("igni")?.show(null)
    }
}

package sh.igniteember.embercode.actions

import com.intellij.notification.NotificationGroupManager
import com.intellij.notification.NotificationType
import com.intellij.openapi.actionSystem.ActionUpdateThread
import com.intellij.openapi.actionSystem.AnAction
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.components.service
import com.intellij.openapi.ui.Messages
import sh.igniteember.embercode.EmberBackendService

/** Confirm + wipe the managed venv + reinstall from scratch. Used
 *  when the cached install is corrupted or out of date in a way the
 *  marker check didn't catch. */
class ReinstallBackendAction : AnAction() {
    override fun getActionUpdateThread(): ActionUpdateThread = ActionUpdateThread.BGT

    override fun update(e: AnActionEvent) {
        e.presentation.isEnabledAndVisible = e.project != null
    }

    override fun actionPerformed(e: AnActionEvent) {
        val project = e.project ?: return
        val choice = Messages.showOkCancelDialog(
            project,
            "Wipe the managed Python cache and re-download uv + Python + ignite-ember?\n\n" +
                "This takes about a minute on a typical connection.",
            "Reinstall the igni Backend",
            "Reinstall",
            "Cancel",
            Messages.getQuestionIcon(),
        )
        if (choice != Messages.OK) return

        project.service<EmberBackendService>().restart(cleanInstall = true)
            .whenComplete { port, err ->
                val group = NotificationGroupManager.getInstance()
                    .getNotificationGroup("igni")
                if (err != null) {
                    group.createNotification(
                        "igni backend reinstall failed",
                        err.message ?: "Unknown error",
                        NotificationType.ERROR,
                    ).notify(project)
                } else {
                    group.createNotification(
                        "igni backend reinstalled",
                        "Fresh managed install ready on port $port.",
                        NotificationType.INFORMATION,
                    ).notify(project)
                }
            }
    }
}

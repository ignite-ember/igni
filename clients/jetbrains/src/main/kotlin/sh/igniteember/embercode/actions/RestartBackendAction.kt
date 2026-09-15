package sh.igniteember.embercode.actions

import com.intellij.notification.NotificationGroupManager
import com.intellij.notification.NotificationType
import com.intellij.openapi.actionSystem.ActionUpdateThread
import com.intellij.openapi.actionSystem.AnAction
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.components.service
import sh.igniteember.embercode.EmberBackendService

/** Kill + relaunch the BE Python process without touching the
 *  managed cache. Quick recovery from BE crashes or stuck sessions. */
class RestartBackendAction : AnAction() {
    override fun getActionUpdateThread(): ActionUpdateThread = ActionUpdateThread.BGT

    override fun update(e: AnActionEvent) {
        e.presentation.isEnabledAndVisible = e.project != null
    }

    override fun actionPerformed(e: AnActionEvent) {
        val project = e.project ?: return
        project.service<EmberBackendService>().restart(cleanInstall = false)
            .whenComplete { port, err ->
                val group = NotificationGroupManager.getInstance()
                    .getNotificationGroup("igni")
                if (err != null) {
                    group.createNotification(
                        "igni backend restart failed",
                        err.message ?: "Unknown error",
                        NotificationType.ERROR,
                    ).notify(project)
                } else {
                    group.createNotification(
                        "igni backend restarted",
                        "Listening on port $port.",
                        NotificationType.INFORMATION,
                    ).notify(project)
                }
            }
    }
}

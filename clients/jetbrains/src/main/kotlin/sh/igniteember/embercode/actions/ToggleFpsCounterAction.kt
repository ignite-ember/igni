package sh.igniteember.embercode.actions

import com.intellij.openapi.actionSystem.ActionUpdateThread
import com.intellij.openapi.actionSystem.AnAction
import com.intellij.openapi.actionSystem.AnActionEvent
import sh.igniteember.embercode.EmberToolWindowFactory

/**
 * Toggles the live-fps overlay inside the igni web view.
 *
 * The overlay lives in the shared web bundle and is normally
 * toggled by ``Cmd+Alt+Shift+F`` on the keyboard, but that path
 * is dead in JCEF — IntelliJ's action-system dispatches on the
 * combo before ``keydown`` ever reaches the DOM. Routing the
 * toggle through an IDE ``AnAction`` fixes this: the action
 * shell forwards to ``EmberToolWindowFactory.toggleFpsOverlay``
 * which executes ``window.__igni_toggleFps()`` in the browser.
 */
class ToggleFpsCounterAction : AnAction() {
    override fun getActionUpdateThread(): ActionUpdateThread = ActionUpdateThread.BGT

    override fun update(e: AnActionEvent) {
        e.presentation.isEnabledAndVisible = e.project != null
    }

    override fun actionPerformed(e: AnActionEvent) {
        val project = e.project ?: return
        EmberToolWindowFactory.toggleFpsOverlay(project)
    }
}

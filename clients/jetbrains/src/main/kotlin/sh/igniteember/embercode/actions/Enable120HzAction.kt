package sh.igniteember.embercode.actions

import com.intellij.openapi.actionSystem.ActionUpdateThread
import com.intellij.openapi.actionSystem.AnAction
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.application.ex.ApplicationManagerEx
import com.intellij.openapi.ui.Messages
import com.intellij.openapi.util.registry.Registry

/**
 * Toggle JCEF between in-process (120 Hz) and out-of-process
 * (default 60 Hz, sandboxed) modes.
 *
 * ## The two states
 *
 * Registry key ``ide.browser.jcef.out-of-process.enabled``:
 * - ``true`` (IDE default on 2025.x): Chromium runs in a separate
 *   ``cef_server`` process. OSR pipeline caps the chat panel at ~60
 *   fps. Chromium crashes stay sandboxed.
 * - ``false``: Chromium runs inside the IDE JVM. Windowed browsers
 *   are supported (see ``EmberToolWindowFactory``), painting on the
 *   display's VSync directly — 120 Hz on ProMotion. Chromium
 *   crashes take the IDE down with them.
 *
 * ``EmberFirstLaunchActivity`` silently flips this to ``false`` on
 * first launch as the plugin's opinionated default. This action
 * gives the user a discoverable way to invert that choice — the
 * label / description update themselves based on the current state
 * so it always reads as "the thing that will happen if I click."
 *
 * ## Restart handling
 *
 * The Registry key is marked ``restartRequired=true`` in the
 * platform defaults; the value change lands immediately but takes
 * effect only on the next IDE launch. We offer Restart Now (via
 * ``ApplicationEx.restart``) or Restart Later — either flips the
 * key; the user's choice is only about timing.
 */
class Enable120HzAction : AnAction() {
    override fun getActionUpdateThread(): ActionUpdateThread = ActionUpdateThread.BGT

    override fun update(e: AnActionEvent) {
        // Registry.is with default=true matches the platform default
        // (out-of-process on by default in 2025.x). ``inProcess`` here
        // means "the plugin has opted into 120 Hz mode."
        val inProcess = !Registry.`is`("ide.browser.jcef.out-of-process.enabled", true)
        e.presentation.text = if (inProcess) {
            "Disable 120 Hz Rendering (Restart)"
        } else {
            "Enable 120 Hz Rendering (Restart)"
        }
        e.presentation.description = if (inProcess) {
            "Switch JCEF back to out-of-process (default). Frame rate " +
                "drops to 60 Hz but Chromium crashes stay sandboxed."
        } else {
            "Switch JCEF to in-process mode so the chat panel paints " +
                "at display refresh instead of the 60 Hz remote-CEF cap."
        }
    }

    override fun actionPerformed(e: AnActionEvent) {
        val project = e.project
        val currentInProcess = !Registry.`is`("ide.browser.jcef.out-of-process.enabled", true)
        val (title, body) = if (currentInProcess) {
            "Disable 120 Hz Rendering" to
                ("This will switch JCEF back to out-of-process mode (the IDE " +
                    "default). The chat panel's frame rate drops to ~60 Hz, " +
                    "but Chromium crashes get sandboxed to a separate process " +
                    "instead of taking the IDE down.\n\n" +
                    "IDE-wide change; requires restart.\n\n" +
                    "Restart the IDE now?")
        } else {
            "Enable 120 Hz Rendering" to
                ("This will switch JCEF to in-process mode so the chat panel " +
                    "can render at your display refresh rate (120 Hz on " +
                    "ProMotion) instead of the 60 Hz cap that JCEF's remote " +
                    "pipeline imposes.\n\n" +
                    "IDE-wide change (affects every JCEF panel — Markdown " +
                    "preview, AI Assistant, etc.). Chromium crashes will " +
                    "bring the IDE down instead of just recycling the " +
                    "sidecar process — the trade-off you accept for the " +
                    "frame rate.\n\n" +
                    "Restart the IDE now?")
        }

        val choice = Messages.showYesNoCancelDialog(
            project,
            body,
            title,
            "Restart Now",
            "Restart Later",
            "Cancel",
            Messages.getQuestionIcon(),
        )

        if (choice == Messages.CANCEL || choice == -1) return

        // Flip: the new value is the *inverse* of the state we
        // observed above. ``currentInProcess`` = true means the key
        // is ``false``; toggling means setting it to ``true`` (back
        // to out-of-process).
        Registry.get("ide.browser.jcef.out-of-process.enabled")
            .setValue(currentInProcess)

        if (choice == Messages.YES) {
            ApplicationManager.getApplication().invokeLater {
                ApplicationManagerEx.getApplicationEx().restart(true)
            }
        }
    }
}

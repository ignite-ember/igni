package sh.igniteember.embercode.actions

import com.intellij.openapi.actionSystem.ActionUpdateThread
import com.intellij.openapi.actionSystem.AnAction
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.ide.CopyPasteManager
import com.intellij.openapi.ui.Messages
import java.awt.datatransfer.StringSelection
import java.nio.file.Files
import sh.igniteember.embercode.EmberRuntime

/**
 * "Diagnose backend" — one-click triage dump.
 *
 * When a user reports "chat gives 'Unknown error'" or "the model
 * selector shows something weird", the useful information to see is:
 *
 *   * which plugin version they're on
 *   * which ``ignite-ember`` version the plugin was pinned to
 *   * which version their venv (or dev override) actually resolves
 *   * the interpreter path — so we can tell managed-venv from
 *     ``EMBER_DEV_BACKEND`` at a glance
 *   * whether ``EMBER_DEV_BACKEND`` / ``IGNITE_EMBER_DEV`` are set
 *   * the marker file's contents — reveals stale-marker cases where
 *     the venv on disk doesn't match what the plugin last installed
 *
 * That's what this action produces. Dumps a plain-text report to a
 * dialog (so the user can eyeball it) and copies it to the clipboard
 * (so they can paste it into the bug report without transcription
 * errors).
 *
 * Deliberately doesn't call the running BE — a broken backend is
 * exactly when this action is useful, so it stays entirely on the
 * plugin/JVM side and only reads local state.
 */
class DoctorAction : AnAction() {
    override fun getActionUpdateThread(): ActionUpdateThread = ActionUpdateThread.BGT

    override fun update(e: AnActionEvent) {
        e.presentation.isEnabledAndVisible = e.project != null
    }

    override fun actionPerformed(e: AnActionEvent) {
        val project = e.project ?: return
        // The version probe spawns a subprocess — keep it off the
        // EDT so the dialog doesn't freeze.
        ApplicationManager.getApplication().executeOnPooledThread {
            val report = buildReport()
            ApplicationManager.getApplication().invokeLater {
                CopyPasteManager.getInstance().setContents(StringSelection(report))
                Messages.showInfoMessage(
                    project,
                    "$report\n\n(This report has been copied to your clipboard — paste " +
                        "it into a bug report so we can see what's happening.)",
                    "igni · Backend Diagnostics",
                )
            }
        }
    }

    private fun buildReport(): String = DoctorReport.render(gatherInputs())

    /** Read every value the report needs from the current process
     *  environment. Kept separate from the render step so tests
     *  don't have to shim env vars / classloader resources / file
     *  I/O — they just construct [DoctorReportInputs] directly. */
    private fun gatherInputs(): DoctorReportInputs {
        val expectedVersion = readVersionProp("ignite-ember-version")
        val pluginVersion = readVersionProp("plugin-version")

        val cache = EmberRuntime.cacheRoot()
        val venvPython = cache.resolve("venv").resolve(EmberRuntime.venvPythonRelPath())
        val markerPath = cache.resolve("ember-install.json")

        val devPath = System.getenv("EMBER_DEV_BACKEND")
        val devAck = System.getenv("IGNITE_EMBER_DEV")
        val devActive = !devPath.isNullOrBlank() &&
            (devAck == "1" || devAck.equals("true", ignoreCase = true))

        val activePython = if (devActive) java.nio.file.Path.of(devPath!!) else venvPython
        val actualVersion = EmberRuntime.probeCliVersion(activePython) ?: "<probe failed>"
        val markerContents = runCatching { Files.readString(markerPath).trim() }
            .getOrElse { "<missing>" }

        return DoctorReportInputs(
            pluginVersion = pluginVersion,
            expectedCli = expectedVersion,
            actualCli = actualVersion,
            interpreterPath = activePython,
            managedVenvPath = venvPython,
            managedVenvPresent = Files.isExecutable(venvPython),
            devPath = devPath,
            devAck = devAck,
            markerPath = markerPath,
            markerContents = markerContents,
        )
    }

    private fun readVersionProp(key: String): String {
        return EmberRuntime::class.java.classLoader
            .getResourceAsStream("META-INF/ember-version.properties")
            ?.use { stream ->
                java.util.Properties().apply { load(stream) }.getProperty(key)
            } ?: "<unknown>"
    }
}

package sh.igniteember.embercode.actions

import java.nio.file.Path

/**
 * Pure formatter for the Doctor report. Kept out of
 * ``DoctorAction`` so tests can exercise every render variant
 * without spawning subprocesses or reading env vars — the action
 * gathers those, this class turns the values into text.
 *
 * All fields are strings so ``null`` / missing / probe-failed
 * cases can be represented directly (``"<unset>"``, ``"<missing>"``,
 * ``"<probe failed>"``) without an extra sentinel type in the
 * data class.
 */
data class DoctorReportInputs(
    val pluginVersion: String,
    val expectedCli: String,
    val actualCli: String,
    val interpreterPath: Path,
    val managedVenvPath: Path,
    val managedVenvPresent: Boolean,
    val devPath: String?,
    val devAck: String?,
    val markerPath: Path,
    val markerContents: String,
) {
    /** ``true`` when the override env var is set AND the ack env var
     *  reads as truthy — the exact rule ``EmberRuntime`` uses to
     *  decide whether to honour ``EMBER_DEV_BACKEND``. */
    val devActive: Boolean
        get() = !devPath.isNullOrBlank() &&
            (devAck == "1" || devAck.equals("true", ignoreCase = true))
}

object DoctorReport {
    fun render(i: DoctorReportInputs): String = buildString {
        appendLine("igni JetBrains plugin · backend diagnostics")
        appendLine("──────────────────────────────────────────")
        appendLine("Plugin version           : ${i.pluginVersion}")
        appendLine("Expected ignite-ember    : ${i.expectedCli}")
        appendLine("Actual ignite-ember      : ${i.actualCli}")
        // Mismatch banner only fires when we have a concrete
        // actual to compare — probe-failed cases shouldn't
        // trigger a scary "MISMATCH" line when the real answer
        // is "we couldn't check".
        if (i.actualCli != i.expectedCli && i.actualCli != "<probe failed>") {
            appendLine("                           ↑ MISMATCH — chat may fail")
        }
        appendLine()
        val source = if (i.devActive) "EMBER_DEV_BACKEND override" else "managed venv"
        appendLine("Backend source           : $source")
        appendLine("Interpreter path         : ${i.interpreterPath}")
        appendLine("Managed venv path        : ${i.managedVenvPath}")
        appendLine("Managed venv present     : ${i.managedVenvPresent}")
        appendLine()
        appendLine("EMBER_DEV_BACKEND        : ${i.devPath ?: "<unset>"}")
        appendLine("IGNITE_EMBER_DEV         : ${i.devAck ?: "<unset>"}")
        // "Override present but ack missing" is the exact
        // footgun the version-gate closes; call it out.
        if (!i.devPath.isNullOrBlank() && !i.devActive) {
            appendLine("                           ↑ set without ack — ignored, using managed venv")
        }
        appendLine()
        appendLine("Marker file              : ${i.markerPath}")
        appendLine("Marker contents          : ${i.markerContents}")
    }
}

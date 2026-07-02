package sh.igniteember.embercode.actions

import java.nio.file.Path
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/**
 * Tests for [DoctorReport.render].
 *
 * The report is what a user pastes into a bug report when the
 * plugin's chat panel starts behaving weirdly. The values it
 * highlights are the ones that historically caused confusion:
 *
 *   * Actual vs expected ``ignite-ember`` version — the whole
 *     reason this subsystem exists.
 *   * Whether an ambient env var is silently redirecting the
 *     managed venv to a stale interpreter.
 *   * Marker file contents — reveals installs that got half-
 *     rolled or manually pip-upgraded.
 *
 * Each test pins one of those signals so a future refactor of
 * the render format can't regress the diagnostic value.
 */
class DoctorReportTest {

    /** Fixture with "everything is fine" values. Individual tests
     *  ``.copy()`` off this and mutate one field to isolate what
     *  they're proving. */
    private val ok = DoctorReportInputs(
        pluginVersion = "0.8.3",
        expectedCli = "0.8.3",
        actualCli = "0.8.3",
        interpreterPath = Path.of("/tmp/ember/venv/bin/python"),
        managedVenvPath = Path.of("/tmp/ember/venv/bin/python"),
        managedVenvPresent = true,
        devPath = null,
        devAck = null,
        markerPath = Path.of("/tmp/ember/ember-install.json"),
        markerContents = "uv=0.5.7;python=3.12;ignite=0.8.3",
    )

    @Test
    fun `no mismatch banner when versions match`() {
        val out = DoctorReport.render(ok)
        // The "MISMATCH" banner is the loudest visual signal in
        // the report. When everything's fine it must NOT appear —
        // false alarms train users to ignore the diagnostic.
        assertFalse(
            out.contains("MISMATCH"),
            "expected no mismatch banner when actualCli == expectedCli; got:\n$out",
        )
        assertTrue(out.contains("Actual ignite-ember      : 0.8.3"))
        assertTrue(out.contains("Backend source           : managed venv"))
    }

    @Test
    fun `mismatch banner fires when actual != expected`() {
        val stale = ok.copy(actualCli = "0.3.8")
        val out = DoctorReport.render(stale)
        // This is the exact configuration that caused the M2.5
        // bug: managed-venv install running an ancient
        // ``ignite-ember`` whose ``defaults.py`` still lists the
        // retired ``MiniMax-M2.5`` model. Report has to shout.
        assertTrue(
            out.contains("MISMATCH"),
            "expected mismatch banner when versions differ; got:\n$out",
        )
        assertTrue(out.contains("Actual ignite-ember      : 0.3.8"))
    }

    @Test
    fun `probe-failed does not trigger a mismatch banner`() {
        val probeFailed = ok.copy(actualCli = "<probe failed>")
        val out = DoctorReport.render(probeFailed)
        // If we couldn't probe the version, saying "MISMATCH" is
        // worse than saying nothing — the answer is "we don't
        // know", not "we know it's wrong". The chip / report
        // shows ``<probe failed>`` verbatim so the reader can
        // tell what happened.
        assertFalse(
            out.contains("MISMATCH"),
            "expected no mismatch banner on probe failure; got:\n$out",
        )
        assertTrue(out.contains("Actual ignite-ember      : <probe failed>"))
    }

    @Test
    fun `dev-override active is called out in Backend source`() {
        val dev = ok.copy(
            devPath = "/usr/local/bin/python3.12",
            devAck = "1",
            interpreterPath = Path.of("/usr/local/bin/python3.12"),
        )
        val out = DoctorReport.render(dev)
        // Even when everything works, the user opted out of the
        // managed venv. Naming the source lets them confirm the
        // override was on purpose (not a stray env var).
        assertTrue(
            out.contains("Backend source           : EMBER_DEV_BACKEND override"),
            "expected dev-override callout; got:\n$out",
        )
    }

    @Test
    fun `override set without ack shows the ignored warning`() {
        val hijack = ok.copy(
            devPath = "/opt/homebrew/bin/python3.12",
            devAck = null, // no ack — ambient env, should be refused
        )
        val out = DoctorReport.render(hijack)
        // The exact footgun the version gate closes: an old
        // ``~/.zshenv`` or ``launchctl setenv`` sets
        // ``EMBER_DEV_BACKEND`` without the user realizing.
        // Report has to explain why the override wasn't honored.
        assertTrue(
            out.contains("set without ack — ignored, using managed venv"),
            "expected 'set without ack — ignored' warning; got:\n$out",
        )
        // ``Backend source`` should still read as managed venv
        // because we refused the override.
        assertTrue(out.contains("Backend source           : managed venv"))
    }

    @Test
    fun `ack accepts case-insensitive 'true'`() {
        // The pure ack rule is duplicated in ``EmberRuntime``'s
        // Kotlin side and in Rust / TypeScript peers; pin the
        // Kotlin classifier here so a future rename doesn't
        // silently flip semantics.
        val dev = ok.copy(
            devPath = "/opt/py",
            devAck = "TRUE",
        )
        val out = DoctorReport.render(dev)
        assertTrue(out.contains("EMBER_DEV_BACKEND override"))
        assertFalse(out.contains("set without ack"))
    }

    @Test
    fun `unset env vars show as unset in the report`() {
        val out = DoctorReport.render(ok)
        // The rendered values matter because bug reporters
        // literally paste this into a ticket. ``<unset>`` is
        // unambiguously "not set" whereas an empty string or a
        // JVM-null looks like a bug in the report itself.
        assertTrue(out.contains("EMBER_DEV_BACKEND        : <unset>"))
        assertTrue(out.contains("IGNITE_EMBER_DEV         : <unset>"))
    }

    @Test
    fun `marker contents are surfaced verbatim`() {
        val stale = ok.copy(markerContents = "uv=0.5.7;python=3.12;ignite=0.5.14")
        val out = DoctorReport.render(stale)
        // Marker file is the record of "what the plugin last
        // successfully installed". Comparing it with the actual
        // and expected versions in the same report is how
        // triagers distinguish "marker corrupted" from
        // "interpreter drifted".
        assertTrue(out.contains("Marker contents          : uv=0.5.7;python=3.12;ignite=0.5.14"))
    }

    @Test
    fun `missing marker file shows as missing`() {
        val fresh = ok.copy(markerContents = "<missing>")
        val out = DoctorReport.render(fresh)
        // Fresh install / cache reset: no marker yet. Report
        // should say so rather than showing a blank field.
        assertTrue(out.contains("Marker contents          : <missing>"))
    }
}

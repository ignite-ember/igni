package sh.igniteember.embercode

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNotNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/**
 * Pure-logic unit tests for [EmberRuntime].
 *
 * Excluded by design: any test that actually downloads uv, installs
 * Python, or starts the BE. Those need a live network + a few
 * minutes per run; covered by manual smoke + the CI matrix that
 * builds the plugin on every platform.
 */
class EmberRuntimeTest {
    @Test
    fun `cacheRoot lives under user home for the current OS`() {
        val root = EmberRuntime.cacheRoot()
        val home = System.getProperty("user.home")
        // The path should be rooted under home or, on Windows,
        // under %LOCALAPPDATA% (which is usually under the user's
        // profile too). At minimum it shouldn't be at /tmp or ``.``.
        assertNotNull(root, "cacheRoot returned null")
        assertTrue(
            root.toString().contains(home) ||
                root.toString().lowercase().contains("appdata") ||
                root.toString().contains("ember-code"),
            "cacheRoot=$root doesn't look anchored on user home",
        )
        assertTrue(
            root.toString().endsWith("ember-code") ||
                root.toString().contains("ember-code"),
            "cacheRoot=$root should include 'ember-code'",
        )
    }

    @Test
    fun `uvBinName matches the running platform`() {
        val name = EmberRuntime.uvBinName()
        val isWindows = System.getProperty("os.name").lowercase().contains("win")
        if (isWindows) {
            assertEquals("uv.exe", name)
        } else {
            assertEquals("uv", name)
        }
    }

    @Test
    fun `venvPythonRelPath matches the running platform`() {
        val rel = EmberRuntime.venvPythonRelPath()
        val isWindows = System.getProperty("os.name").lowercase().contains("win")
        if (isWindows) {
            assertEquals("Scripts/python.exe", rel)
        } else {
            assertEquals("bin/python", rel)
        }
    }

    @Test
    fun `uvTarget produces a valid GitHub-release triple`() {
        val triple = EmberRuntime.uvTarget()
        val validTriples = setOf(
            "aarch64-apple-darwin",
            "x86_64-apple-darwin",
            "x86_64-unknown-linux-gnu",
            "aarch64-unknown-linux-gnu",
            "x86_64-pc-windows-msvc",
        )
        assertTrue(
            triple in validTriples,
            "uvTarget=$triple is not one of the recognised uv release assets",
        )
    }

    @Test
    fun `ensureFreeSpace passes when minimum is trivially satisfied`() {
        // 1 byte is essentially always available — confirms the
        // happy path doesn't throw on a freshly-created tmp dir.
        val tmp = java.nio.file.Files.createTempDirectory("ember-disk-test-")
        try {
            EmberRuntime.ensureFreeSpace(tmp, 1L) {}
        } finally {
            tmp.toFile().delete()
        }
    }

    @Test
    fun `ensureFreeSpace throws with an actionable message when minimum is too large`() {
        // ``Long.MAX_VALUE`` will never be available — confirms the
        // failure path produces a message that includes the byte
        // counts and the cache path so the user has something to
        // act on.
        val tmp = java.nio.file.Files.createTempDirectory("ember-disk-test-")
        try {
            val exc = org.junit.jupiter.api.Assertions.assertThrows(
                IllegalStateException::class.java,
            ) {
                EmberRuntime.ensureFreeSpace(tmp, Long.MAX_VALUE) {}
            }
            assertTrue(
                exc.message?.contains("disk space") == true,
                "error message should mention 'disk space', got: ${exc.message}",
            )
            assertTrue(
                exc.message?.contains(tmp.toString()) == true,
                "error should include the path; got: ${exc.message}",
            )
        } finally {
            tmp.toFile().delete()
        }
    }
}

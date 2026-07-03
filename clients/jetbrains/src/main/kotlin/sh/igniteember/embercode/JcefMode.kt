package sh.igniteember.embercode

/**
 * Detect JCEF's "remote / split" mode — the out-of-process
 * ``cef_server`` architecture that became default in 2025.x IDE
 * builds. See ``EmberToolWindowFactory`` for the rendering-mode
 * implications and ``Enable120HzAction`` /
 * ``EmberFirstLaunchActivity`` for the user-facing surface that
 * lets a user opt back into in-process JCEF for higher frame
 * rates.
 *
 * ``JBCefApp.isRemoteEnabled`` is package-private in the 2024.2.4
 * platform we compile against (still package-private in 2025.3),
 * so we reach it reflectively. Fallback on any failure is
 * ``false`` — worst case the plugin asks for a windowed browser
 * on an in-process JCEF, which just works.
 */
internal fun isRemoteCefEnabled(): Boolean = try {
    val cls = Class.forName("com.intellij.ui.jcef.JBCefApp")
    val m = cls.getDeclaredMethod("isRemoteEnabled")
    m.isAccessible = true
    (m.invoke(null) as? Boolean) ?: false
} catch (_: Throwable) {
    false
}

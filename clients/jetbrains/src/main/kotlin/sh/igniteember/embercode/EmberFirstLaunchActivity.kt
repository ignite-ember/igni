package sh.igniteember.embercode

import com.intellij.ide.util.PropertiesComponent
import com.intellij.notification.NotificationGroupManager
import com.intellij.notification.NotificationType
import com.intellij.openapi.project.Project
import com.intellij.openapi.startup.ProjectActivity
import com.intellij.openapi.util.registry.Registry

/**
 * Opinionated first-launch policy: silently flip JCEF to in-process
 * mode so the chat panel renders at the display refresh rate (120 Hz
 * on ProMotion) instead of the ~60 Hz cap remote-CEF's OSR pipeline
 * imposes.
 *
 * ## What actually happens on first launch
 *
 * Every 2025.x IDE ships remote/split JCEF as the default. If we
 * detect that mode AND we haven't touched the setting before, we
 * flip ``ide.browser.jcef.out-of-process.enabled`` to ``false`` and
 * post a passive notification explaining what changed. The setting's
 * ``restartRequired=true`` means the effect kicks in on the next
 * natural IDE launch — no forced restart. Users who dislike the
 * change can revert via ``Tools → igni → Disable 120 Hz Rendering``
 * (see ``Enable120HzAction``, which reads the current state and
 * relabels itself as an enable/disable toggle).
 *
 * ## Why opinionated instead of asking
 *
 * The plugin exists to make the chat panel feel good. Under remote-
 * CEF the panel visibly stutters (60 → 30 fps in busy scrolls, jitter
 * on streaming markdown). Asking every user "would you like the
 * frame rate we spent all this effort on?" adds friction for a
 * choice the user's answer is almost always yes for. Users who need
 * remote-CEF's sandbox isolation (Chromium crash quarantine) are a
 * tiny minority — we keep the escape hatch for them.
 *
 * ## Idempotency & respect for user choice
 *
 * The ``ASKED_KEY`` flag records "we already touched this setting
 * once". Set on every path — auto-flip, no-op, or "already in-process"
 * — so:
 * - A user who disables via the Tools action after we enabled won't
 *   see us re-enable on the next launch (we're marked as done).
 * - A user who was already on in-process JCEF (JB 2024.2 or someone
 *   who had flipped the key manually earlier) doesn't get a
 *   surprise notification either.
 */
class EmberFirstLaunchActivity : ProjectActivity {

    override suspend fun execute(project: Project) {
        val props = PropertiesComponent.getInstance()
        if (props.getBoolean(TOUCHED_KEY, false)) return

        // Regardless of what we decide below, mark this as our one
        // shot so a subsequent project open (in a multi-window
        // session) doesn't try to flip again — races safely because
        // ``PropertiesComponent`` is app-scoped and read-modify-write
        // happens on the same thread inside a project-open activity.
        props.setValue(TOUCHED_KEY, true)

        // Already in-process — nothing to do, no notification. Might
        // be an older IDE (in-process by default) or a user who
        // pre-flipped the setting.
        if (!isRemoteCefEnabled()) return

        // Silent flip. The setting is Registry-backed, restart-
        // required — landing the value now means the *next* IDE
        // launch runs JCEF in-process. No forced restart; users
        // pick up 120 Hz whenever they next relaunch naturally.
        Registry.get("ide.browser.jcef.out-of-process.enabled").setValue(false)

        NotificationGroupManager.getInstance()
            .getNotificationGroup(NOTIFICATION_GROUP)
            .createNotification(
                "igni: 120 Hz rendering enabled",
                "JCEF is now in-process so the chat panel paints at your display's " +
                    "refresh rate on the next IDE restart. To revert to the default " +
                    "60 Hz sandboxed mode, use Tools → igni → Disable 120 Hz Rendering.",
                NotificationType.INFORMATION,
            )
            .notify(project)
    }

    companion object {
        /** Application-scoped flag: "we've already made our one-time
         *  decision about the JCEF mode." Versioned so we can force
         *  a re-evaluation later (bump ``v1`` → ``v2``) if the policy
         *  changes. */
        private const val TOUCHED_KEY = "sh.igniteember.embercode.jcefMode.touched.v1"

        /** Reuse the plugin's existing notification group — one
         *  entry in Preferences → Notifications for everything
         *  igni-emitted. */
        private const val NOTIFICATION_GROUP = "EmberCode"
    }
}

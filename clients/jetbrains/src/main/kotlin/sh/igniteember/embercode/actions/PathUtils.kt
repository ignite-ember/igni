package sh.igniteember.embercode.actions

/**
 * Shared path helpers for the action layer.
 *
 * The "absolute → project-relative" idiom appears in
 * ``AddFileToChatAction`` and ``AddSelectionToChatAction`` — both
 * resolve a ``VirtualFile`` and want to show the user a short
 * relative path in the chat pill, falling back to the absolute path
 * when the file lives outside the project root (a common case for
 * generated files, libraries opened from "Go to declaration",
 * etc.).
 *
 * Kept as a top-level helper rather than an extension on
 * ``VirtualFile`` so the test suite doesn't have to stand up an
 * IntelliJ Platform fixture to exercise it.
 */
internal object PathUtils {
    /**
     * Return [absolutePath] relative to [basePath] if it lives under
     * the project root; otherwise return [absolutePath] unchanged.
     *
     * Matches the contract the actions relied on inline:
     * * a path that is exactly the base + ``/`` + suffix strips both
     *   the base and the separator,
     * * a path that doesn't have the base as a directory prefix is
     *   returned as-is (no accidental partial-prefix stripping —
     *   e.g. ``/home/usr/foobar/file`` against base ``/home/usr/foo``
     *   does NOT become ``bar/file``),
     * * a null/empty base passes through.
     */
    fun projectRelative(absolutePath: String, basePath: String?): String {
        if (basePath.isNullOrEmpty()) return absolutePath
        val prefix = if (basePath.endsWith('/')) basePath else "$basePath/"
        return if (absolutePath.startsWith(prefix)) {
            absolutePath.removePrefix(prefix)
        } else {
            absolutePath
        }
    }
}

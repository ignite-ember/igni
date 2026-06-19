package sh.igniteember.embercode

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Test
import sh.igniteember.embercode.actions.PathUtils

/**
 * Tests for the project-relative path helper extracted from the
 * "Add to Ember Chat" actions.
 *
 * Why pinned: a subtle bug would show as wrong file labels in chat
 * pills — easy to overlook in casual review, surfaces as confusing
 * "where is this file?" reports from users.
 */
class PathUtilsTest {
    @Test
    fun `strips base + slash when path lives under project root`() {
        assertEquals(
            "src/main/foo.kt",
            PathUtils.projectRelative(
                "/home/dev/project/src/main/foo.kt",
                "/home/dev/project",
            ),
        )
    }

    @Test
    fun `returns absolute when path is outside project root`() {
        // Common case: user clicks "Go to declaration" on a library
        // type, opening a file at ``~/.gradle/caches/...``. That path
        // doesn't live under ``project.basePath`` and should appear
        // verbatim in the chat pill.
        assertEquals(
            "/home/dev/.gradle/caches/some.jar!/com/foo/Bar.class",
            PathUtils.projectRelative(
                "/home/dev/.gradle/caches/some.jar!/com/foo/Bar.class",
                "/home/dev/project",
            ),
        )
    }

    @Test
    fun `does NOT strip on partial-prefix collisions`() {
        // ``/home/dev/foobar/file`` vs base ``/home/dev/foo`` —
        // ``startsWith("foo")`` would match but we want directory-
        // boundary matching. Catches the bug where ``"foo".length``
        // chars are stripped from an unrelated path.
        assertEquals(
            "/home/dev/foobar/file.kt",
            PathUtils.projectRelative(
                "/home/dev/foobar/file.kt",
                "/home/dev/foo",
            ),
        )
    }

    @Test
    fun `tolerates trailing slash on base path`() {
        // ``project.basePath`` is usually stripped of its trailing
        // slash, but defensive against IDE versions / VFS quirks.
        assertEquals(
            "src/main.kt",
            PathUtils.projectRelative(
                "/home/dev/project/src/main.kt",
                "/home/dev/project/",
            ),
        )
    }

    @Test
    fun `null base returns path unchanged`() {
        // ``Project.basePath`` is nullable — IntelliJ allows "default"
        // projects with no on-disk root.
        assertEquals(
            "/abs/path/file.kt",
            PathUtils.projectRelative("/abs/path/file.kt", null),
        )
    }

    @Test
    fun `empty base returns path unchanged`() {
        assertEquals(
            "/abs/path/file.kt",
            PathUtils.projectRelative("/abs/path/file.kt", ""),
        )
    }

    @Test
    fun `path identical to base returns absolute (no zero-length pill)`() {
        // If the user somehow ends up with the project root itself
        // as the file ref, returning ``""`` would render an empty
        // pill — keep the absolute path so something useful shows.
        assertEquals(
            "/home/dev/project",
            PathUtils.projectRelative("/home/dev/project", "/home/dev/project"),
        )
    }
}

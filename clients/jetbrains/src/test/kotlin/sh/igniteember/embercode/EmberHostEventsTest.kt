package sh.igniteember.embercode

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import sh.igniteember.embercode.actions.EmberHostEvents

/**
 * Tests the inline JSON escaper in [EmberHostEvents].
 *
 * Why hand-rolled instead of a JSON library: this is the ONLY string
 * the plugin emits into the JCEF webview as raw JS, and pulling in a
 * full JSON dep just to escape it would bloat the plugin jar. The
 * tradeoff is paying down with tests on the corner cases that bite —
 * quotes, backslashes, control characters, newlines.
 */
class EmberHostEventsTest {
    @Test
    fun `unchanged when input has no escape-needing chars`() {
        assertEquals(
            "hello world",
            EmberHostEvents.jsonEscape("hello world"),
        )
    }

    @Test
    fun `doubles backslashes`() {
        assertEquals(
            "a\\\\b",
            EmberHostEvents.jsonEscape("a\\b"),
        )
    }

    @Test
    fun `escapes embedded double quotes`() {
        assertEquals(
            "a\\\"b",
            EmberHostEvents.jsonEscape("a\"b"),
        )
    }

    @Test
    fun `escapes newlines, carriage returns, tabs`() {
        assertEquals(
            "a\\nb\\rc\\td",
            EmberHostEvents.jsonEscape("a\nb\rc\td"),
        )
    }

    @Test
    fun `escapes other control characters as unicode escapes`() {
        // Form feed (0x0C) is a control char that's not in the
        // shortcut set; should become a  sequence.
        assertEquals(
            "a\\u000cb",
            EmberHostEvents.jsonEscape("ab"),
        )
    }

    @Test
    fun `leaves Unicode letters intact (only control chars + special escapes are touched)`() {
        assertEquals(
            "café — flame",
            EmberHostEvents.jsonEscape("café — flame"),
        )
    }

    // ── composerPayload — JSON shape sent to the FE on ⌘E ─────────────

    @Test
    fun `composerPayload includes all fields when provided`() {
        val payload = EmberHostEvents.composerPayload(
            path = "src/main.kt",
            text = "fun foo() { }",
            startLine = 10,
            endLine = 12,
        )
        // The FE's chat composer parses this as JSON and renders a
        // code-paste pill. Field order matters only for readability;
        // contents must include every label the FE keys off.
        assertEquals(
            """{"text":"fun foo() { }","path":"src/main.kt","line":10,"end_line":12}""",
            payload,
        )
    }

    @Test
    fun `composerPayload omits path when null (untitled buffer case)`() {
        // Selections in an unsaved buffer have no on-disk path —
        // the FE renders a pill without a file label rather than
        // showing ``null`` / an empty string.
        val payload = EmberHostEvents.composerPayload(
            path = null,
            text = "scratch text",
            startLine = 1,
            endLine = 1,
        )
        assertFalse("\"path\":" in payload, "path key must not appear: $payload")
        assertTrue("\"text\":\"scratch text\"" in payload, payload)
    }

    @Test
    fun `composerPayload omits line range when missing`() {
        // When the action has text but no derivable line range (rare;
        // future caller might pass plain text without geometry), the
        // ``line`` / ``end_line`` keys are simply absent.
        val payload = EmberHostEvents.composerPayload(
            path = "x.kt",
            text = "bar",
        )
        assertEquals("""{"text":"bar","path":"x.kt"}""", payload)
    }

    @Test
    fun `composerPayload escapes embedded quotes + newlines in text`() {
        // The text comes straight from the editor; a snippet like
        // ``println("hello\nworld")`` would otherwise break the
        // payload's JSON shape. Pinning this is the single most
        // important property of the helper.
        val payload = EmberHostEvents.composerPayload(
            path = "x.kt",
            text = "println(\"a\nb\")",
        )
        // ``\"`` for the quote, ``\n`` for the newline.
        assertEquals(
            """{"text":"println(\"a\nb\")","path":"x.kt"}""",
            payload,
        )
    }

    // ── attachFilePayload — JSON shape sent on right-click → attach ──

    @Test
    fun `attachFilePayload wraps the path in a single-key object`() {
        assertEquals(
            """{"path":"src/foo.kt"}""",
            EmberHostEvents.attachFilePayload("src/foo.kt"),
        )
    }

    @Test
    fun `attachFilePayload escapes paths with spaces and quotes`() {
        // Windows users routinely have ``C:\Users\Some Name\proj``;
        // backslashes have to be doubled in the JSON string.
        assertEquals(
            """{"path":"C:\\Users\\Some \"Quoted\" Name\\file.kt"}""",
            EmberHostEvents.attachFilePayload(
                """C:\Users\Some "Quoted" Name\file.kt""",
            ),
        )
    }
}

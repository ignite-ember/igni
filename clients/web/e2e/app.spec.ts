/**
 * End-to-end tests for the real chat app surface (no ``?demo=team``).
 *
 * Each test runs against a fresh ``FixtureBackend`` that speaks the
 * actual WS wire format. This catches bugs in ``protocol/client.ts``,
 * ``chat/model.ts``, ``App.tsx``'s push-event router, and the
 * connection-state machine — none of which the demo suite exercises.
 *
 * The fixture deliberately implements the BARE MINIMUM the FE needs
 * to boot (Welcome + sane defaults for the half-dozen RPCs fired on
 * mount). Tests script per-method overrides when they want a specific
 * branch.
 */

import { test, expect } from "./fixtures/embed";

test.describe("connection lifecycle", () => {
  test("client opens, exchanges welcome, populates session id", async ({
    page,
    backend,
    appUrl,
  }) => {
    await page.goto(appUrl);
    // Composer's placeholder flips from "Connecting…" to the real
    // prompt once ``state === connected``. Same signal a user sees.
    await expect(page.locator(".composer-editable")).toHaveAttribute(
      "data-placeholder",
      /Message Ember/,
    );
    // FE issues several RPCs on attach — at minimum get_session_id
    // (or attach_session) + count_context_tokens + get_status. We
    // just check the connection landed; the exact RPC list is an
    // implementation detail that shifts as the app evolves.
    await expect.poll(() => backend.clientCount()).toBe(1);
  });

  test("server crash → composer flips to 'Connecting…'", async ({
    page,
    backend,
    appUrl,
  }) => {
    await page.goto(appUrl);
    await expect.poll(() => backend.clientCount()).toBe(1);

    // Simulate BE crash by closing the server. The FE's onclose
    // path treats it as a normal disconnect (not the 1008
    // "replaced" code, which is BE-initiated and platform-specific)
    // and emits ``disconnected``, which the composer renders as
    // the "Connecting…" placeholder.
    await backend.close();
    await expect(page.locator(".composer-editable")).toHaveAttribute(
      "data-placeholder",
      /Connecting/,
      { timeout: 10_000 },
    );
  });
});

test.describe("user message round-trip", () => {
  test("typing + Enter sends a user_message envelope to the BE", async ({
    page,
    backend,
    appUrl,
  }) => {
    await page.goto(appUrl);
    const editor = page.locator(".composer-editable");
    await expect(editor).toHaveAttribute("data-placeholder", /Message Ember/);

    await editor.click();
    await editor.type("hello fixture");
    await editor.press("Enter");

    // The user message envelope should land in inbound traffic.
    // ``user_message`` is the FE-side type per protocol/messages.ts.
    await expect
      .poll(() =>
        backend.received().some(
          (m) => m.type === "user_message" && String(m.text) === "hello fixture",
        ),
      )
      .toBe(true);

    // FE renders an optimistic user bubble immediately.
    await expect(page.locator(".msg-user").last()).toContainText("hello fixture");
  });

  test("BE streams content_delta → assistant bubble renders the text", async ({
    page,
    backend,
    appUrl,
  }) => {
    await page.goto(appUrl);
    await expect(page.locator(".composer-editable")).toHaveAttribute(
      "data-placeholder",
      /Message Ember/,
    );

    const editor = page.locator(".composer-editable");
    await editor.click();
    await editor.type("ping");
    await editor.press("Enter");

    // Find the user_message id the FE just sent — we'll echo it back
    // on every streamed envelope so the client demultiplexer routes
    // them to the same handler.
    await expect
      .poll(() => backend.received().find((m) => m.type === "user_message"))
      .toBeTruthy();
    const userMsg = backend.received().find((m) => m.type === "user_message")!;
    const runId = String(userMsg.id);

    // Server side: stream three deltas then stream_end.
    backend.pushEvent({ type: "content_delta", id: runId, text: "Hello, " });
    backend.pushEvent({ type: "content_delta", id: runId, text: "world!" });
    backend.pushEvent({ type: "stream_end", id: runId });

    // The assistant bubble should pick up the concatenated text.
    // Use a CSS class scoped to assistant messages (msg-assistant)
    // and target the last one so we don't trip on earlier bubbles.
    await expect(page.locator(".msg-assistant").last()).toContainText(
      "Hello, world!",
    );
  });
});

test.describe("push notifications", () => {
  test("status_update push refreshes the footer model name", async ({
    page,
    backend,
    appUrl,
  }) => {
    // Start with a known status the FE consumes on first poll.
    backend.onRpc("get_status", () => ({
      type: "status_update",
      model: "test-model-v1",
      context_tokens: 0,
      max_context: 100_000,
      cloud_connected: false,
      cloud_org: "",
    }));

    await page.goto(appUrl);
    // Composer's model chip displays the current model. Wait for it
    // to settle on the initial value.
    await expect(page.locator(".composer-model").first()).toContainText(
      "test-model-v1",
      { timeout: 10_000 },
    );

    // Push an update — chip text should change without a refresh.
    backend.pushEvent({
      type: "status_update",
      model: "test-model-v2",
      context_tokens: 0,
      max_context: 100_000,
      cloud_connected: false,
      cloud_org: "",
    });
    await expect(page.locator(".composer-model").first()).toContainText(
      "test-model-v2",
    );
  });

  test("file_edited push routes to host.notifyFileEdited (no crash on web)", async ({
    page,
    backend,
    appUrl,
  }) => {
    // The web host has no native editor — ``host.notifyFileEdited``
    // no-ops. The test asserts the push DOESN'T crash the app: the
    // composer stays interactive after dispatch. Catches the regression
    // class where a bad message handler tears the React tree down.
    await page.goto(appUrl);
    await expect(page.locator(".composer-editable")).toHaveAttribute(
      "data-placeholder",
      /Message Ember/,
    );

    backend.pushEvent({
      type: "push_notification",
      channel: "file_edited",
      payload: { path: "src/foo.py" },
    });

    // Give the dispatch a tick to fire, then verify the composer's
    // still responsive.
    await page.waitForTimeout(100);
    await page.locator(".composer-editable").click();
    await page.locator(".composer-editable").type("post-edit");
    await expect(page.locator(".composer-editable")).toContainText("post-edit");
  });
});

test.describe("session bootstrap", () => {
  test("history loaded from get_chat_history renders on mount", async ({
    page,
    backend,
    appUrl,
  }) => {
    // Pre-populate the BE with a couple of "previous" messages.
    // The FE calls ``get_chat_history`` (App.tsx:334) right after
    // resolving the session id; shape is {role, content, run_id}.
    backend.onRpc("get_chat_history", () => [
      { role: "user", content: "what's 2+2?", run_id: "r-1" },
      { role: "assistant", content: "It's 4.", run_id: "r-1" },
    ]);

    await page.goto(appUrl);
    // The two restored bubbles should be present.
    await expect(page.locator(".msg-user")).toContainText("what's 2+2?", {
      timeout: 10_000,
    });
    await expect(page.locator(".msg-assistant")).toContainText("It's 4.");
  });

  test("custom session id from attach_session lands on the client", async ({
    page,
    backend,
    appUrl,
  }) => {
    backend.onRpc("get_session_id", () => "fixture-sess-xyz");
    await page.goto(appUrl);

    // The composer becomes interactive once the session id is set
    // (App.tsx awaits ``get_session_id`` before letting the user
    // type). If it never resolves we hang at "Connecting…".
    await expect(page.locator(".composer-editable")).toHaveAttribute(
      "data-placeholder",
      /Message Ember/,
      { timeout: 10_000 },
    );
    // The session chip in the footer displays a short prefix of the
    // session id. Confirm the value flowed end-to-end.
    await expect(page.locator(".session-chip")).toContainText(/fixture-sess/);
  });
});

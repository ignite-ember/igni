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

  test("two pages bound to different sessions only render their own session's events", async ({
    backend,
    browser,
    appUrl,
  }) => {
    // Drives the FE-side composition of the multi-session contract:
    // each tab binds to its own ``session_id`` via ``get_session_id``;
    // every inbound broadcast is filtered by the FE's WS client
    // (``protocol/client.ts`` ~ line 430) so a tab only renders events
    // stamped for *its* session. Catches a class of "session-id drift"
    // regressions — e.g. the FE forgetting to set ``sessionId`` after
    // attach, or the filter accidentally matching empty stamps.
    //
    // We rely on the existing fixture (one WS server, broadcasts to
    // all attached clients) and override ``get_session_id`` to hand
    // out distinct ids per connection.
    const handed: string[] = [];
    const POOL = ["sess-alpha", "sess-beta"];
    backend.onRpc("get_session_id", () => {
      const id = POOL[handed.length] ?? POOL[POOL.length - 1];
      handed.push(id);
      return id;
    });
    // Initial composer-model text differs per page only because the
    // session-stamped pushes below land. The fixture's default
    // ``get_status`` answer is shared.
    backend.onRpc("get_status", () => ({
      type: "status_update",
      model: "initial-model",
      context_tokens: 0,
      max_context: 200_000,
      cloud_connected: false,
      cloud_org: "",
    }));

    // Each page in its own browser context → independent
    // localStorage / IndexedDB → independent ``client_id``, so they
    // both hit ``get_session_id`` fresh (no leaked SESSION_KEY).
    // Fresh contexts get no inherited ``baseURL``; pass it explicitly
    // so the ``?ws=…`` relative path resolves to the dev server.
    const ctxA = await browser.newContext({ baseURL: "http://127.0.0.1:5179" });
    const ctxB = await browser.newContext({ baseURL: "http://127.0.0.1:5179" });
    try {
      const pageA = await ctxA.newPage();
      const pageB = await ctxB.newPage();

      await pageA.goto(appUrl);
      await expect(pageA.locator(".composer-model").first()).toContainText(
        "initial-model",
        { timeout: 10_000 },
      );
      await pageB.goto(appUrl);
      await expect(pageB.locator(".composer-model").first()).toContainText(
        "initial-model",
        { timeout: 10_000 },
      );

      // Both pages connected and each pulled a distinct session id.
      await expect.poll(() => backend.clientCount()).toBe(2);
      await expect.poll(() => handed.length).toBeGreaterThanOrEqual(2);
      expect(new Set(handed.slice(0, 2))).toEqual(new Set(POOL));

      // Push a status_update stamped for "sess-alpha" — only pageA
      // (bound to that session) should render it. pageB must keep the
      // initial model since the FE's session filter drops the event.
      backend.pushEvent({
        type: "status_update",
        session_id: "sess-alpha",
        model: "model-alpha",
        context_tokens: 0,
        max_context: 200_000,
        cloud_connected: false,
        cloud_org: "",
      });
      await expect(pageA.locator(".composer-model").first()).toContainText(
        "model-alpha",
      );
      // pageB still shows the initial value — a regression would let
      // alpha's update bleed in.
      await expect(pageB.locator(".composer-model").first()).toContainText(
        "initial-model",
      );

      // Now the mirror: stamp for "sess-beta" — only pageB should pick
      // it up; pageA stays on model-alpha.
      backend.pushEvent({
        type: "status_update",
        session_id: "sess-beta",
        model: "model-beta",
        context_tokens: 0,
        max_context: 200_000,
        cloud_connected: false,
        cloud_org: "",
      });
      await expect(pageB.locator(".composer-model").first()).toContainText(
        "model-beta",
      );
      await expect(pageA.locator(".composer-model").first()).toContainText(
        "model-alpha",
      );
    } finally {
      await ctxA.close();
      await ctxB.close();
    }
  });

  test("two pages bound to the same session both render its events (mirroring)", async ({
    backend,
    browser,
    appUrl,
  }) => {
    // Two tabs of the same project (e.g. user duplicated the window):
    // every event for "shared" must paint on BOTH. Catches the
    // opposite-of-isolation regression — a stamping rule that
    // accidentally drops valid same-session events.
    backend.onRpc("get_session_id", () => "shared-session");
    backend.onRpc("get_status", () => ({
      type: "status_update",
      model: "initial-shared",
      context_tokens: 0,
      max_context: 200_000,
      cloud_connected: false,
      cloud_org: "",
    }));

    // Fresh contexts get no inherited ``baseURL``; pass it explicitly
    // so the ``?ws=…`` relative path resolves to the dev server.
    const ctxA = await browser.newContext({ baseURL: "http://127.0.0.1:5179" });
    const ctxB = await browser.newContext({ baseURL: "http://127.0.0.1:5179" });
    try {
      const pageA = await ctxA.newPage();
      const pageB = await ctxB.newPage();
      await pageA.goto(appUrl);
      await pageB.goto(appUrl);
      await expect.poll(() => backend.clientCount()).toBe(2);
      await expect(pageA.locator(".composer-model").first()).toContainText(
        "initial-shared",
        { timeout: 10_000 },
      );
      await expect(pageB.locator(".composer-model").first()).toContainText(
        "initial-shared",
        { timeout: 10_000 },
      );

      backend.pushEvent({
        type: "status_update",
        session_id: "shared-session",
        model: "shared-updated",
        context_tokens: 0,
        max_context: 200_000,
        cloud_connected: false,
        cloud_org: "",
      });
      // Both tabs reflect the push.
      await expect(pageA.locator(".composer-model").first()).toContainText(
        "shared-updated",
      );
      await expect(pageB.locator(".composer-model").first()).toContainText(
        "shared-updated",
      );
    } finally {
      await ctxA.close();
      await ctxB.close();
    }
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

test.describe("first-launch onboarding", () => {
  /**
   * The "new user opens the app for the first time" path: empty
   * history, empty session list, default model. They type → send →
   * see streaming → see the assistant bubble settle. This single
   * test exercises every layer the FE will hit on day one and
   * surfaces regressions that wouldn't show up in the isolated
   * unit-style tests.
   */
  test("empty-history greeting → first message → streamed reply", async ({
    page,
    backend,
    appUrl,
  }) => {
    // Pre-fixture state: brand-new user.
    backend.onRpc("get_chat_history", () => []);
    backend.onRpc("get_pending_messages", () => []);
    backend.onRpc("list_sessions", () => ({ sessions: [] }));

    await page.goto(appUrl);

    // The composer's placeholder switches once we're connected —
    // that's the visible signal a new user gets that the panel is
    // ready to receive input.
    await expect(page.locator(".composer-editable")).toHaveAttribute(
      "data-placeholder",
      /Message Ember/,
      { timeout: 10_000 },
    );

    // No prior chat items rendered.
    await expect(page.locator(".msg-user")).toHaveCount(0);
    await expect(page.locator(".msg-assistant")).toHaveCount(0);

    // Send the first message.
    const editor = page.locator(".composer-editable");
    await editor.click();
    await editor.type("hello, are you working?");
    await editor.press("Enter");

    // FE renders the optimistic user bubble immediately.
    await expect(page.locator(".msg-user").last()).toContainText(
      "hello, are you working?",
    );

    // BE saw the envelope.
    await expect
      .poll(() =>
        backend
          .received()
          .find(
            (m) =>
              m.type === "user_message" &&
              String(m.text).includes("hello, are you working?"),
          ),
      )
      .toBeTruthy();

    const runId = String(
      backend.received().find((m) => m.type === "user_message")!.id,
    );

    // Simulate a typical streamed reply: deltas then the standard
    // run-completion handshake the BE emits at end of turn.
    backend.pushEvent({
      type: "content_delta",
      id: runId,
      text: "Yes, ",
    });
    backend.pushEvent({
      type: "content_delta",
      id: runId,
      text: "I'm here and ready.",
    });
    backend.pushEvent({
      type: "run_completed",
      id: runId,
      run_id: runId,
      parent_run_id: null,
      input_tokens: 12,
      output_tokens: 5,
      reasoning_tokens: 0,
      duration: 1.2,
    });
    backend.pushEvent({ type: "stream_end", id: runId });

    // Assistant bubble lands with the concatenated text.
    await expect(page.locator(".msg-assistant").last()).toContainText(
      "Yes, I'm here and ready.",
    );

    // After ``stream_end`` the composer becomes interactive again —
    // verify by typing a follow-up (would be blocked if the FE
    // thought a run was still in flight).
    await editor.click();
    await editor.type("good");
    await expect(editor).toContainText("good");
  });

  /**
   * Tests the "wires connected, BE never replies" failure mode.
   * If a real BE crashes between accepting the message and
   * producing any output, the FE has to remain usable instead of
   * locking up the composer forever. Catches the regression class
   * where ``stream_end`` is the only thing that unblocks input.
   */
  test("BE accepts then dies — composer recovers after disconnect", async ({
    page,
    backend,
    appUrl,
  }) => {
    await page.goto(appUrl);
    await expect(page.locator(".composer-editable")).toHaveAttribute(
      "data-placeholder",
      /Message Ember/,
      { timeout: 10_000 },
    );

    const editor = page.locator(".composer-editable");
    await editor.click();
    await editor.type("hi");
    await editor.press("Enter");

    // Wait for the envelope to arrive at the BE so we know the
    // run is in flight before we kill the connection.
    await expect
      .poll(() => backend.received().find((m) => m.type === "user_message"))
      .toBeTruthy();

    // BE crashes mid-run.
    await backend.close();

    // Composer placeholder reverts to the "Connecting…" state —
    // that's the user-visible signal the app survived the crash
    // rather than wedging.
    await expect(page.locator(".composer-editable")).toHaveAttribute(
      "data-placeholder",
      /Connecting/,
      { timeout: 10_000 },
    );
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
    // The session chip in the footer displays only the FIRST 8
    // CHARS of the id (see ``StatusBits.tsx:_short``). The full
    // id rides on the ``title`` attribute as ``Copy <id>`` for
    // click-to-copy. Check both: the truncated text proves the
    // chip rendered, the title proves the WHOLE id flowed
    // end-to-end without silent truncation upstream.
    const chip = page.locator(".session-chip");
    await expect(chip).toContainText("fixture-");
    await expect(chip).toHaveAttribute("title", /Copy fixture-sess-xyz/);
  });
});

/**
 * Real-BE smoke test: spawn the actual Python backend, drive the FE
 * against it, exercise one round-trip. Catches wire-format drift
 * between the Python emitter and the TypeScript decoder that the
 * JS-fixture suite cannot see (their schemas evolve independently).
 *
 * The backend comes from ``fixtures/live-be.ts``, which spawns one
 * whenever the project venv exists. It used to be gated on
 * ``IGNI_E2E_REAL_BE=1``, and the docstring claimed "CI sets that var
 * on hosts with the venv prepared" — CI had to be told to, twice, for
 * two different variables. A capability check needs telling once. F128.
 *
 * What's covered:
 *   - The BE prints its ``{"status":"ready","ws_port":N}`` envelope
 *     on stdout and binds an actual loopback WS server.
 *   - The FE's ``protocol/client.ts`` decoder accepts the Welcome and
 *     every RPC reply the boot flow produces (get_session_id,
 *     get_status, get_client_state, …) without throwing on an
 *     unknown field or a missing default.
 *   - The composer reaches the "ready" placeholder, which is the
 *     same end-state real users see post-connect.
 *
 * What's NOT covered (deliberately): the agent loop. Driving an
 * actual ``run_message`` would call the LLM — out of scope for a
 * wire-format smoke. The cancel/HITL/long-stream paths are tested
 * separately in the Python-side integration suite.
 */

import { test, expect } from "./fixtures/live-be";

test.describe("real BE wire format", () => {
  test("boot → connected: FE decodes every RPC the real BE returns", async ({
    page,
    liveBe,
  }) => {
    // FE talks to the real loopback BE via ``?ws=`` query param.
    await page.goto(`/?ws=${encodeURIComponent(liveBe.wsUrl)}`);

    // The composer's placeholder is set by the connection state
    // machine — flipping from "Connecting…" to "Message Ember"
    // means: WS open, Welcome consumed, every boot RPC's reply
    // decoded without throwing. If the BE emitted any envelope the
    // FE schema can't parse, the connection-ready transition
    // wouldn't fire and this would timeout.
    await expect(page.locator(".composer-editable")).toHaveAttribute(
      "data-placeholder",
      /Message (Ember|igni)/,
      { timeout: 30_000 },
    );

    // The model chip shows whatever the real BE put in status_update
    // — we only assert it's *something* (the registry depends on
    // local config) and not the "—" placeholder a missing reply
    // would leave. Catches a regression where status_update arrives
    // but the FE drops the model field due to a schema mismatch.
    const modelText = await page
      .locator(".composer-model")
      .first()
      .textContent({ timeout: 10_000 });
    expect(modelText).toBeTruthy();
    expect(modelText?.trim()).not.toBe("");
    expect(modelText?.trim()).not.toBe("—");
  });
});

/**
 * The window is a view of a model the host owns.
 *
 * `docs/HOST_OBJECT_MODEL.md`, *The UI is part of the model*: the page a
 * window is on, its sidebar, the composer, toasts, dialogs — all of it is
 * the window's model, kept by the backend. igni's views bind to it. So
 * an extension writing the model (`host.ui`) changes what the window
 * shows, and what the person does in the window is a write to the model
 * — which the backend signals.
 *
 * The fixture backend keeps window models the way the real one does
 * (`e2e/fixtures/backend.ts`), and stands in for an extension writing
 * one. The backend's half — that a write is a signal, that a dialog's
 * answer reaches the extension — is `tests/test_a_window_is_a_view_of_its_model.py`.
 */

import { test, expect } from "./fixtures/embed";
import type { FixtureBackend } from "./fixtures/backend";
import type { Page } from "@playwright/test";

async function openWindow(page: Page, backend: FixtureBackend, appUrl: string): Promise<string> {
  await page.goto(appUrl);
  await expect(page.locator(".composer-editable")).toHaveAttribute(
    "data-placeholder",
    /Message (Ember|igni)/,
  );
  await expect.poll(() => backend.windows.ids().length).toBe(1);
  return backend.windows.ids()[0];
}

/** The window's own writes of one field, in order. */
function writesOf(backend: FixtureBackend, window: string, field: string): unknown[] {
  return backend.windows
    .changes()
    .filter((c) => c.window === window && c.field === field && c.token.startsWith(`${window}#`))
    .map((c) => c.value);
}

test.describe("an extension's write changes the window", () => {
  test("the page, the sidebar and the composer follow the model", async ({
    page,
    backend,
    appUrl,
  }) => {
    const window = await openWindow(page, backend, appUrl);

    backend.windows.extensionWrites(window, "route", [
      { kind: "plugins", label: "Plugins", params: {} },
    ]);
    await expect(page.locator("section.page")).toHaveAttribute("aria-label", "Plugins");
    backend.windows.extensionWrites(window, "route", []);
    await expect(page.locator("section.page")).toHaveCount(0);

    backend.windows.extensionWrites(window, "sidebar", {
      open: false,
      width: null,
      section: null,
    });
    await expect(page.locator("nav.sidebar")).toHaveClass(/closed/);

    backend.windows.extensionWrites(window, "composer", {
      text: "from the reviewer",
      mode: "chat",
      attachments: [],
    });
    await expect(page.locator(".composer-editable")).toHaveText("from the reviewer");
  });

  test("a toast shows, and dismissing it is the window's write", async ({
    page,
    backend,
    appUrl,
  }) => {
    const window = await openWindow(page, backend, appUrl);
    const id = backend.windows.extensionToasts(window, { text: "Review ready", body: "Two issues" });
    await expect(page.locator(".toast-title")).toHaveText("Review ready");
    await page.getByLabel("Dismiss").click();
    await expect(page.locator(".toast")).toHaveCount(0);
    await expect
      .poll(() =>
        backend
          .received()
          .some(
            (m) =>
              m.method === "window_model" &&
              (m.args as Record<string, unknown>).op === "dismiss_toast" &&
              (m.args as Record<string, unknown>).value === id,
          ),
      )
      .toBe(true);
  });

  test("a dialog asks the person, and their answer goes back to the model", async ({
    page,
    backend,
    appUrl,
  }) => {
    const window = await openWindow(page, backend, appUrl);
    const id = backend.windows.extensionAsks(window, {
      title: "Ship it?",
      body: "The review found nothing.",
      buttons: [
        { id: "no", label: "Wait" },
        { id: "yes", label: "Ship", primary: true },
      ],
      input: { label: "Note", value: "lgtm" },
    });
    const dialog = page.getByRole("dialog");
    await expect(dialog).toContainText("Ship it?");
    // Labelled as the extension's, never igni's own question.
    await expect(dialog).toContainText("from acme.reviewer");
    await dialog.locator("input").fill("ship on Friday");
    await dialog.getByRole("button", { name: "Ship" }).click();
    await expect(dialog).toHaveCount(0);
    await expect
      .poll(() => backend.windows.changes().find((c) => c.event === "dialog_closed"))
      .toMatchObject({
        window,
        item: { id },
        answer: { button: "yes", value: "ship on Friday" },
      });
  });
});

test.describe("what the person does is a write to the model", () => {
  test("navigating, the sidebar and typing reach the window's model", async ({
    page,
    backend,
    appUrl,
  }) => {
    const window = await openWindow(page, backend, appUrl);

    await page.locator("nav.sidebar").getByRole("button", { name: "Plugins" }).click();
    await expect
      .poll(() => writesOf(backend, window, "route").at(-1))
      .toEqual([{ kind: "plugins", label: "Plugins" }]);
    await page.locator("button.page-crumb", { hasText: "Chat" }).click();
    await expect.poll(() => writesOf(backend, window, "route").at(-1)).toEqual([]);

    const before = (backend.windows.state(window).sidebar as { open: boolean }).open;
    await page.getByTitle("Toggle sessions").click();
    await expect
      .poll(() => (backend.windows.state(window).sidebar as { open: boolean }).open)
      .toBe(!before);

    const editor = page.locator(".composer-editable");
    await editor.click();
    await editor.type("half a thought");
    // After the composer's short pause, the draft is the model's.
    await expect
      .poll(() => (backend.windows.state(window).composer as { text: string }).text)
      .toBe("half a thought");
  });
});

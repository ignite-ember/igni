// @vitest-environment jsdom
/**
 * Component tests for ``BackendVersionChip``.
 *
 * The chip has three visual states + a "render nothing" fallback,
 * and it reads its inputs from the URL query params the Tauri shell
 * sets. What we pin:
 *
 *   • No params → renders nothing (Tauri without params, dev
 *     browser).
 *   • Matching versions + managed venv → neutral ``tone-ok`` chip.
 *   • Dev-override active → ``tone-warn``.
 *   • Mismatched versions → ``tone-danger``.
 *   • ``<meta>`` tags alone render nothing.
 *
 * The subprocess probe ``__version__`` returns "unknown" when it
 * fails — the chip renders "<probe failed>" instead of a version
 * string so the reader sees the diagnostic gap, not a fake number.
 */

import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { BackendVersionChip } from "./StatusBits";

/** Pin the URL search string for the duration of a single test.
 *  Restores the original on cleanup so tests don't leak into each
 *  other's globals. */
function withSearch(query: string, run: () => void) {
  const original = window.location.search;
  // ``window.location`` is read-only, but jsdom lets us override
  // via ``Object.defineProperty`` per test. Restore afterward.
  Object.defineProperty(window, "location", {
    configurable: true,
    value: { ...window.location, search: query },
    writable: true,
  });
  try {
    run();
  } finally {
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { ...window.location, search: original },
      writable: true,
    });
  }
}

/** Inject a set of meta tags into ``document.head`` for the
 *  duration of a test. Removes them on cleanup. */
function withMetas(metas: Record<string, string>, run: () => void) {
  const nodes: HTMLMetaElement[] = [];
  for (const [name, content] of Object.entries(metas)) {
    const el = document.createElement("meta");
    el.setAttribute("name", name);
    el.setAttribute("content", content);
    document.head.appendChild(el);
    nodes.push(el);
  }
  try {
    run();
  } finally {
    for (const n of nodes) n.remove();
  }
}

afterEach(() => {
  cleanup();
  // Belt-and-suspenders — every test should clean up its own
  // side effects via ``withSearch`` / ``withMetas`` but a stray
  // meta from a failed test would otherwise leak into later ones.
  document
    .querySelectorAll(
      'meta[name="ember-expected-cli"], meta[name="ember-actual-cli"], meta[name="ember-backend-source"]',
    )
    .forEach((el) => el.remove());
});

describe("BackendVersionChip — visibility", () => {
  it("renders nothing when no source provides the params", () => {
    withSearch("", () => {
      const { container } = render(<BackendVersionChip />);
      // Empty container = component returned ``null``. This is
      // the common case for plain browsers / dev previews.
      expect(container.firstChild).toBeNull();
    });
  });

  it("renders nothing when only two of three params are present", () => {
    // Half-embedded params are treated as absent — a partial
    // signal is worse than none, since it'd render a chip with
    // undefined text and could mislead the reader.
    withSearch("?expected_cli=0.8.3&actual_cli=0.8.3", () => {
      const { container } = render(<BackendVersionChip />);
      expect(container.firstChild).toBeNull();
    });
  });
});

describe("BackendVersionChip — tone", () => {
  it("renders tone-ok when actual == expected AND managed venv", () => {
    withSearch("?expected_cli=0.8.3&actual_cli=0.8.3&backend_source=managed_venv", () => {
      render(<BackendVersionChip />);
      const chip = screen.getByText(/v0\.8\.3/);
      // Neutral tone class — least visually loud; the reader
      // shouldn't be alarmed by a "everything is fine" chip.
      expect(chip.className).toContain("tone-ok");
      expect(chip.className).not.toContain("tone-warn");
      expect(chip.className).not.toContain("tone-danger");
    });
  });

  it("renders tone-warn when the dev-override is active", () => {
    // Even if the versions happen to match, running through
    // ``IGNI_DEV_BACKEND`` is worth flagging — the user opted
    // out of the managed venv and might not remember they did.
    withSearch("?expected_cli=0.8.3&actual_cli=0.8.3&backend_source=dev_override", () => {
      render(<BackendVersionChip />);
      const chip = screen.getByText(/v0\.8\.3/);
      expect(chip.className).toContain("tone-warn");
    });
  });

  it("renders tone-danger when actual != expected", () => {
    // The bug the whole feature exists to prevent: user thinks
    // they're on v0.8.3 but the backend is really v0.3.8.
    withSearch("?expected_cli=0.8.3&actual_cli=0.3.8&backend_source=managed_venv", () => {
      render(<BackendVersionChip />);
      const chip = screen.getByText(/v0\.3\.8/);
      expect(chip.className).toContain("tone-danger");
    });
  });

  it("renders '<probe failed>' when actual is 'unknown'", () => {
    // Runtime probe timed out or the interpreter is missing —
    // show the diagnostic gap rather than pretending we know
    // what's installed. Reader still sees the chip, still can
    // click "Diagnose Backend" for the full report.
    withSearch("?expected_cli=0.8.3&actual_cli=unknown&backend_source=managed_venv", () => {
      render(<BackendVersionChip />);
      // Explicit — no "v" prefix, no fake number.
      expect(screen.getByText("<probe failed>")).toBeTruthy();
    });
  });
});

describe("BackendVersionChip — input sources", () => {
  it("ignores <meta> tags: the URL is the only source", () => {
    // The meta-tag path belonged to the VS Code webview, which igni no
    // longer ships. A page carrying the tags and no params shows nothing.
    withMetas(
      {
        "ember-expected-cli": "0.8.3",
        "ember-actual-cli": "0.8.3",
        "ember-backend-source": "managed_venv",
      },
      () => {
        const { container } = render(<BackendVersionChip />);
        expect(container.innerHTML).toBe("");
      },
    );
  });
});

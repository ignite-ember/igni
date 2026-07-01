// @vitest-environment jsdom
/**
 * Tests for ``ThemeToggle`` — single-button theme cycler
 * (auto → light → dark → auto).
 *
 * Three side-effects to lock down:
 *   1. ``<html data-theme>`` attribute drives the CSS palette
 *   2. localStorage persists the choice across reloads
 *   3. In ``auto`` mode, ``data-os-prefers-light`` mirrors the
 *      OS preference so CSS doesn't need a @media rule to fight
 *      against an explicit theme.
 *
 * One thing NOT tested here: the auto-mode matchMedia ``change``
 * listener — jsdom's matchMedia stub doesn't dispatch real
 * change events. The listener-wiring path is exercised; the
 * actual OS-flips-mid-session behaviour would need an
 * integration/manual check.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render } from "@testing-library/react";

// Node 25 ships a native localStorage stub that's an empty object
// with no methods, which masks jsdom's Storage implementation.
// Install a real in-memory shim per test so the module's
// ``localStorage.getItem`` / ``setItem`` calls actually work.
function installLocalStorageShim(): Storage {
  const data = new Map<string, string>();
  const shim: Storage = {
    get length() {
      return data.size;
    },
    clear() {
      data.clear();
    },
    getItem(key: string) {
      return data.has(key) ? (data.get(key) as string) : null;
    },
    key(i: number) {
      return Array.from(data.keys())[i] ?? null;
    },
    removeItem(key: string) {
      data.delete(key);
    },
    setItem(key: string, value: string) {
      data.set(key, String(value));
    },
  };
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    writable: true,
    value: shim,
  });
  return shim;
}

beforeEach(() => {
  installLocalStorageShim();
  delete document.documentElement.dataset.theme;
  delete document.documentElement.dataset.osPrefersLight;
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.resetModules();
});

async function fresh() {
  // Reset the module so its top-level ``applyTheme(readStored())``
  // re-runs against the test's fresh DOM/localStorage state.
  vi.resetModules();
  return await import("./ThemeToggle");
}

describe("ThemeToggle — initial state", () => {
  it("defaults to auto when localStorage is empty", async () => {
    const { ThemeToggle } = await fresh();
    const { container } = render(<ThemeToggle />);
    // ``data-theme`` set on <html> from the module's top-level
    // ``applyTheme(readStored())`` call.
    expect(document.documentElement.dataset.theme).toBe("auto");
    expect(
      container.querySelector("button")?.getAttribute("title"),
    ).toMatch(/Theme: auto/);
  });

  it("reads light from localStorage on load", async () => {
    window.localStorage.setItem("ember:theme", "light");
    const { ThemeToggle } = await fresh();
    const { container } = render(<ThemeToggle />);
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(
      container.querySelector("button")?.getAttribute("title"),
    ).toMatch(/Theme: light/);
  });

  it("reads dark from localStorage on load", async () => {
    window.localStorage.setItem("ember:theme", "dark");
    const { ThemeToggle } = await fresh();
    render(<ThemeToggle />);
    expect(document.documentElement.dataset.theme).toBe("dark");
  });

  it("falls back to auto for an unknown stored value (forward-compat)", async () => {
    // A future version might add new theme names; until the
    // user picks one we know, ignore the stored value rather
    // than throwing.
    window.localStorage.setItem("ember:theme", "sepia");
    const { ThemeToggle } = await fresh();
    render(<ThemeToggle />);
    expect(document.documentElement.dataset.theme).toBe("auto");
  });
});

describe("ThemeToggle — cycle on click", () => {
  it("auto → light", async () => {
    const { ThemeToggle } = await fresh();
    const { container } = render(<ThemeToggle />);
    fireEvent.click(container.querySelector("button")!);
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(window.localStorage.getItem("ember:theme")).toBe("light");
  });

  it("light → dark → auto (full cycle)", async () => {
    window.localStorage.setItem("ember:theme", "light");
    const { ThemeToggle } = await fresh();
    const { container } = render(<ThemeToggle />);
    const btn = container.querySelector("button")!;
    fireEvent.click(btn);
    expect(document.documentElement.dataset.theme).toBe("dark");
    fireEvent.click(btn);
    expect(document.documentElement.dataset.theme).toBe("auto");
  });

  it("title hint forecasts the NEXT step (not the current state)", async () => {
    // The tooltip says "click for X" — what happens next, not
    // what state you're in. Helps the user predict the
    // outcome of the click rather than puzzle over what they
    // already see.
    const { ThemeToggle } = await fresh();
    const { container } = render(<ThemeToggle />);
    expect(
      container.querySelector("button")?.getAttribute("title"),
    ).toMatch(/click for light/);
    fireEvent.click(container.querySelector("button")!);
    expect(
      container.querySelector("button")?.getAttribute("title"),
    ).toMatch(/click for dark/);
  });
});

describe("ThemeToggle — auto-mode OS preference mirror", () => {
  function installMatchMediaShim(matches: (query: string) => boolean) {
    // jsdom doesn't ship a ``matchMedia`` implementation, so the
    // module's ``window.matchMedia?.(\"…\")`` call would normally
    // return undefined. Install a real one per test so the OS-pref
    // branch actually runs.
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      writable: true,
      value: (query: string) =>
        ({
          matches: matches(query),
          media: query,
          addEventListener: vi.fn(),
          removeEventListener: vi.fn(),
          addListener: vi.fn(),
          removeListener: vi.fn(),
          dispatchEvent: vi.fn(),
          onchange: null,
        }) as unknown as MediaQueryList,
    });
  }

  it("sets data-os-prefers-light=1 when OS prefers light", async () => {
    // The CSS uses ``[data-theme=\"auto\"][data-os-prefers-light=\"1\"]``
    // to pick the light palette without colliding with explicit
    // overrides.
    installMatchMediaShim((q) => q.includes("light"));
    const { ThemeToggle } = await fresh();
    render(<ThemeToggle />);
    expect(document.documentElement.dataset.osPrefersLight).toBe("1");
  });

  it("sets data-os-prefers-light=0 when OS prefers dark", async () => {
    installMatchMediaShim(() => false);
    const { ThemeToggle } = await fresh();
    render(<ThemeToggle />);
    expect(document.documentElement.dataset.osPrefersLight).toBe("0");
  });

  it("removes the data-os-prefers-light attr in light/dark mode", async () => {
    // The mirror only exists for auto — once the user picks an
    // explicit theme, the OS preference is no longer relevant
    // and shouldn't shadow the chosen palette.
    window.localStorage.setItem("ember:theme", "light");
    const { ThemeToggle } = await fresh();
    render(<ThemeToggle />);
    expect(document.documentElement.dataset.osPrefersLight).toBeUndefined();
  });
});

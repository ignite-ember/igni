import { useEffect, useState } from "react";

/**
 * Live FPS readout — small bottom-left overlay showing the running
 * frame rate from a ``requestAnimationFrame`` loop.
 *
 * Present-time verification of the Tauri ``macos-fps`` plugin,
 * the JCEF windowed-mode experiment, and any other pipeline change
 * that claims to affect frame delivery. The number moves in real
 * time so a user can scroll / type / trigger animations and watch
 * the readout track — no need to open a Web Inspector or run a
 * console snippet.
 *
 * Sampling window is 500 ms so the number is jitter-smoothed but
 * still reflects short stalls. Tone bands are advisory: green for
 * ProMotion-native rates, amber for 60-ish Hz, red for anything
 * lower (the useful "we're dropping frames" signal).
 */
export function FPSCounter() {
  const [fps, setFps] = useState(0);

  useEffect(() => {
    let handle = 0;
    let last = performance.now();
    let frames = 0;
    const loop = (t: number) => {
      frames++;
      const delta = t - last;
      // 500 ms window — long enough to average out sub-frame
      // jitter, short enough that a stall is visible within a
      // second of it happening.
      if (delta >= 500) {
        setFps(Math.round((frames * 1000) / delta));
        frames = 0;
        last = t;
      }
      handle = requestAnimationFrame(loop);
    };
    handle = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(handle);
  }, []);

  const tone = fps >= 100 ? "ok" : fps >= 55 ? "warn" : "danger";

  return (
    <div className={`fps-counter fps-counter-${tone}`} aria-hidden="true">
      {fps} <span className="fps-counter-unit">fps</span>
    </div>
  );
}

/**
 * Keyboard-toggleable wrapper around [FPSCounter]. Hidden by
 * default. Press ``Ctrl+Alt+Shift+F`` (or ``Cmd+Alt+Shift+F`` on
 * macOS — either modifier prefix is accepted) to flip the
 * overlay on / off. State persists in ``localStorage`` so it
 * survives page reloads without leaking into a shared user's
 * next session (per-origin scope; VSCode webviews and JCEF
 * panels each keep their own).
 *
 * All-four-modifiers key is deliberate: it can't collide with
 * any IDE / browser shortcut in JetBrains, VSCode, or the
 * standalone Tauri build. Users who want the counter know the
 * combo (documented in the plugin README); everyone else never
 * sees it.
 */
const FPS_STORAGE_KEY = "ember_fps_visible";

function readFpsVisible(): boolean {
  try {
    return localStorage.getItem(FPS_STORAGE_KEY) === "1";
  } catch {
    // localStorage can throw in some sandboxes (VSCode webview
    // with strict CSP, private-browsing modes). Silently fall
    // back to "hidden" — the shortcut still works in-session.
    return false;
  }
}

function writeFpsVisible(v: boolean) {
  try {
    localStorage.setItem(FPS_STORAGE_KEY, v ? "1" : "0");
  } catch {
    /* see readFpsVisible */
  }
}

export function FPSCounterOverlay() {
  const [visible, setVisible] = useState<boolean>(() => readFpsVisible());

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // Require ALL of shift + alt + (ctrl or meta). Accepting
      // either ctrl OR meta covers Windows/Linux (Ctrl) and
      // macOS (Cmd) with one binding.
      const modOk = e.shiftKey && e.altKey && (e.ctrlKey || e.metaKey);
      if (!modOk) return;
      // Use ``e.code`` not ``e.key`` — on macOS the Option
      // modifier remaps the character (``Alt+F`` produces ``ƒ``
      // via the dead-key layer), so ``e.key === "f"`` never
      // matches. ``e.code`` is the physical key identifier
      // and is unaffected by modifiers.
      if (e.code !== "KeyF") return;
      e.preventDefault();
      setVisible((prev) => {
        const next = !prev;
        writeFpsVisible(next);
        return next;
      });
    };
    // ``capture: true`` so we see the keystroke before any
    // component-level handler (composer, chat search, etc.)
    // that might stop propagation.
    window.addEventListener("keydown", onKey, { capture: true });
    return () => window.removeEventListener("keydown", onKey, { capture: true });
  }, []);

  return visible ? <FPSCounter /> : null;
}

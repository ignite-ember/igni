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

const FPS_STORAGE_KEY = "ember_fps_visible";
const FPS_TOGGLE_EVENT = "ember:toggle-fps";

function readFpsVisible(): boolean {
  try {
    return localStorage.getItem(FPS_STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}

function writeFpsVisible(v: boolean) {
  try {
    localStorage.setItem(FPS_STORAGE_KEY, v ? "1" : "0");
  } catch {
    /* localStorage may throw under strict-CSP webviews */
  }
}

// Hidden by default. Toggled by:
//  - ``Ctrl/Cmd + Alt + Shift + F`` in surfaces where the web
//    view actually receives the ``keydown`` (Tauri, plain
//    browser, VSCode webview).
//  - ``window.__igni_toggleFps()`` (a global installed on the
//    ``window`` object at mount time) from IntelliJ's action
//    system in the JB plugin, where JCEF swallows the
//    keystroke before the DOM ever sees it. See
//    ``ToggleFpsCounterAction`` in the JB plugin.
// State persists in ``localStorage`` per-origin.
export function FPSCounterOverlay() {
  const [visible, setVisible] = useState<boolean>(() => readFpsVisible());

  useEffect(() => {
    const toggle = () => {
      setVisible((prev) => {
        const next = !prev;
        writeFpsVisible(next);
        return next;
      });
    };

    const onKey = (e: KeyboardEvent) => {
      const modOk = e.shiftKey && e.altKey && (e.ctrlKey || e.metaKey);
      if (!modOk) return;
      // ``e.code`` — physical key. macOS remaps ``Alt+F`` to
      // ``ƒ`` at the character level, so ``e.key === "f"`` fails.
      if (e.code !== "KeyF") return;
      e.preventDefault();
      toggle();
    };

    const onCustomEvent = () => toggle();

    // Install the global entrypoint so JB's ``AnAction`` can
    // reach us via ``JBCefBrowser.cefBrowser.executeJavaScript``.
    const w = window as unknown as { __igni_toggleFps?: () => void };
    w.__igni_toggleFps = toggle;

    window.addEventListener("keydown", onKey, { capture: true });
    window.addEventListener(FPS_TOGGLE_EVENT, onCustomEvent);
    return () => {
      window.removeEventListener("keydown", onKey, { capture: true });
      window.removeEventListener(FPS_TOGGLE_EVENT, onCustomEvent);
      if (w.__igni_toggleFps === toggle) delete w.__igni_toggleFps;
    };
  }, []);

  return visible ? <FPSCounter /> : null;
}

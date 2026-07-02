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

// Always-on wrapper for now. The earlier keyboard-toggle path
// (``Cmd+Alt+Shift+F``) didn't fire in JCEF — the plugin's
// action system likely swallows the combo before it reaches the
// web view's ``keydown`` listener. Making it opt-in through an
// IntelliJ ``AnAction`` that toggles a CSS class on the JCEF
// document is the proper fix; that's follow-up work.
export function FPSCounterOverlay() {
  return <FPSCounter />;
}

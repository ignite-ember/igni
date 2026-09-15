/**
 * Which session a window adopts on connect.
 *
 * A window opened by New Window carries the folder it was opened on
 * as a `?dir=` param, and binds a session there via
 * `attach_session({project_dir})`. The first window carries no `dir`
 * and lands on the backend's default session, which is already the
 * launch folder.
 *
 * Two cases make this more than an if/else, and both are about the
 * window label being an identity that outlives a single app run
 * (labels are `main`, `w-2`, `w-3`… and reused lowest-free):
 *
 *  - **Reload.** The `dir` param survives a reload, so a naive
 *    "dir means bind a folder" would mint a NEW session every time
 *    the user hits refresh. Once bound, the stored session wins.
 *
 *  - **A label reused across app runs.** Last run's `w-2` was opened
 *    on repo A and stored that session; this run's `w-2` was opened
 *    on repo B. The stored session is real but belongs somewhere
 *    else, and adopting it would silently show repo A in a window the
 *    user pointed at repo B. So the stored *folder* is compared, not
 *    just the stored session id.
 *
 * When the stored session does match the requested folder, the resume
 * passes `project_dir` alongside `session_id`. That matters: the
 * backend's bare-`session_id` path refuses a session whose registered
 * directory differs from the directory the *backend* was launched
 * with (`session_orchestrator.py`), which is every window bound to a
 * second repo. Passing the folder takes the branch that re-registers
 * it — idempotent when it hasn't changed — and resumes in place.
 */

export type SessionBindingPlan =
  /** First bind of a window opened onto a folder. */
  | { kind: "bind-folder"; projectDir: string }
  /** Reload of a window already bound to that folder. */
  | { kind: "resume-in-folder"; sessionId: string; projectDir: string }
  /** No folder of its own; restore what this client had. */
  | { kind: "resume"; sessionId: string }
  /** Nothing stored — take the backend's default session. */
  | { kind: "default" };

export function planSessionBinding(input: {
  /** The `?dir=` param, `""` when the window carries none. */
  wantedDir: string;
  /** `client_state` session id for this window, `""` if unset. */
  storedSession: string;
  /** `client_state` project dir for this window, `""` if unset. */
  storedDir: string;
}): SessionBindingPlan {
  const { wantedDir, storedSession, storedDir } = input;

  if (wantedDir) {
    // Both must line up. A stored session with no stored folder is
    // pre-multi-window state (or a write that didn't land) — treat it
    // as unknown rather than assuming it belongs here.
    if (storedSession && storedDir === wantedDir) {
      return { kind: "resume-in-folder", sessionId: storedSession, projectDir: wantedDir };
    }
    return { kind: "bind-folder", projectDir: wantedDir };
  }

  if (storedSession) {
    return { kind: "resume", sessionId: storedSession };
  }
  return { kind: "default" };
}

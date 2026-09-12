import { Drawer } from "../panels/Drawer";

/**
 * What `/codeindex` says when the code index is switched off.
 *
 * The menu entry, the completion and the help listing are all gone in
 * this state — but a command that has been removed from every list can
 * still be typed, and it lands here. A page that rendered nothing, or
 * rendered a status panel for a subsystem that is not running, would
 * both be worse than one sentence explaining the situation.
 *
 * Deliberately not a status panel. There is no index, no sync and no
 * coverage to report, and the whole point of the surrounding work was
 * that instruments reading zero look like a template rather than an
 * answer.
 *
 * `code_index.enabled` is off, which is a kill switch rather than a
 * preference: the agent is never offered the CodeIndex tool, it runs
 * the plain `main_agent` prompt instead of the CodeIndex-first
 * variant, and the Neo4j sidecar is not attached. It is usually set by
 * an org group, whose settings sit above anything a developer can pass
 * on the command line — so "ask your administrator" is the accurate
 * advice rather than a brush-off.
 */
export function CodeIndexDisabled({ onClose }: { onClose: () => void }) {
  return (
    <Drawer title="CodeIndex" onClose={onClose}>
      <div className="codeindex-empty">
        <p className="codeindex-empty-lead">
          <strong>Code indexing is turned off for this session.</strong> Semantic code
          search is unavailable, and the agent works from the files it reads directly
          rather than from an index.
        </p>
        <p className="codeindex-empty-where">
          This is set in configuration — usually by your organisation's group policy,
          which takes precedence over local settings.
        </p>
      </div>
    </Drawer>
  );
}

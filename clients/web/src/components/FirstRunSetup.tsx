/**
 * What a fresh install sees before it has a model.
 *
 * Every capability the welcome screen advertises — agents, skills,
 * workflows, loops — comes back `No model configured. Run /login…`
 * until this is done. The screen used to offer all eight of them and
 * not this, so the only useful action on a first run was the one action
 * missing.
 *
 * There is deliberately **one** button. A "use my own model" button was
 * here and opened the composer's model menu, which reads its entries
 * from `models.registry` — empty on exactly the installs that see this
 * block, so it opened an empty dropdown every time. Bringing your own
 * model means editing a file, so the note says which file.
 *
 * Its own component so it can be tested without standing up ``App``,
 * which has no tests.
 */

export function FirstRunSetup({ onLogin }: { onLogin: () => void }) {
  return (
    <div className="welcome-setup">
      <p className="welcome-setup-lead">One step first: igni needs a model to run.</p>
      <div className="welcome-setup-actions">
        <button className="btn btn-primary" onClick={onLogin}>
          Log in to igni Cloud
        </button>
      </div>
      {/* The path is `.ember`, not `.igni` — the product name in prose
          and the directory on disk are not the same string, and a note
          that sends someone to edit a file nothing reads is worse than
          no note. */}
      <p className="welcome-setup-note">
        Logging in discovers hosted models for you. To bring your own, add an entry to{" "}
        <code>models.registry</code> in <code>~/.ember/config.yaml</code>.
      </p>
    </div>
  );
}

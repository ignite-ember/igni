import { Drawer } from "../panels/Drawer";
import { BUILTIN_COMMANDS, type SlashCommand } from "../Composer";

/**
 * Every command, grouped.
 *
 * This was `/help` building a markdown list of all twenty-eight
 * commands plus every installed skill and handing it to a 620px modal.
 * It is the only complete index of what the product can do and the
 * first thing a new user opens, and it was arriving as one flat
 * alphabetical-by-accident wall.
 *
 * Grouped by what you are trying to do rather than by what the command
 * touches, because someone reading this does not yet know what the
 * subsystems are called — that is why they are here.
 */
const GROUPS: { title: string; blurb: string; commands: string[] }[] = [
  {
    title: "This conversation",
    blurb: "Start over, trim it down, or find out what is filling it up.",
    commands: ["/clear", "/compact", "/ctx", "/rename", "/fork"],
  },
  {
    title: "Getting around",
    blurb: "Move between conversations, or come back here.",
    commands: ["/sessions", "/help", "/quit"],
  },
  {
    title: "What the agent can reach",
    blurb: "The tools, knowledge and extensions available to a run.",
    commands: ["/agents", "/skills", "/plugins", "/plugin", "/mcp", "/knowledge", "/codeindex", "/hooks"],
  },
  {
    title: "Running work",
    blurb: "Repeat a prompt, or hand work to the background.",
    commands: ["/loop", "/workflows", "/schedule", "/evals"],
  },
  {
    title: "How much it may do on its own",
    blurb: "Permission modes, from proposing to acting unattended.",
    commands: ["/plan", "/accept", "/bypass"],
  },
  {
    title: "Setup",
    blurb: "The model, the account, and the rest of the settings.",
    commands: [
      "/model",
      "/login",
      "/logout",
      "/whoami",
      "/config",
      "/memory",
      "/output-style",
      "/sync-knowledge",
      "/bug",
    ],
  },
];

/** Commands in `GROUPS` that no longer exist, and commands that exist
 *  in no group — either is a list that has drifted from the code. */
export function groupingDrift(commands: SlashCommand[]): {
  missing: string[];
  ungrouped: string[];
} {
  const known = new Set(commands.map((c) => c.name));
  const placed = new Set(GROUPS.flatMap((g) => g.commands));
  return {
    missing: [...placed].filter((name) => !known.has(name)).sort(),
    ungrouped: [...known].filter((name) => !placed.has(name)).sort(),
  };
}

export function HelpPage({
  skills,
  onRun,
  onClose,
}: {
  /** Skills are user- and plugin-supplied, so they cannot be grouped
   *  by hand — they get their own section. */
  skills: SlashCommand[];
  /** Drop a command into the composer rather than firing it. Reading
   *  help is not the same as deciding to run something, and several
   *  of these take arguments. */
  onRun: (name: string) => void;
  onClose: () => void;
}) {
  const byName = new Map(BUILTIN_COMMANDS.map((c) => [c.name, c]));

  // Anything the groups above have not placed still has to appear —
  // a command missing from help because nobody updated a list here is
  // worse than one in the wrong section.
  const { ungrouped } = groupingDrift(BUILTIN_COMMANDS);
  const sections = [
    ...GROUPS.map((g) => ({ ...g, commands: g.commands.filter((n) => byName.has(n)) })),
    ...(ungrouped.length
      ? [{ title: "Other", blurb: "", commands: ungrouped }]
      : []),
  ];

  return (
    <Drawer title="Help" onClose={onClose}>
      <div className="help-intro">
        Type <code>/</code> in the composer to search these, <code>@</code> for files, and{" "}
        <code>$</code> to run a shell command.
      </div>

      {sections.map((group) => (
        <section className="help-group" key={group.title}>
          <h3 className="help-group-title">{group.title}</h3>
          {group.blurb && <p className="help-group-blurb">{group.blurb}</p>}
          <div className="help-commands">
            {group.commands.map((name) => (
              <button className="help-command" key={name} onClick={() => onRun(name)}>
                <code className="help-command-name">{name}</code>
                <span className="help-command-desc">{byName.get(name)?.description}</span>
              </button>
            ))}
          </div>
        </section>
      ))}

      {skills.length > 0 && (
        <section className="help-group">
          <h3 className="help-group-title">Skills</h3>
          <p className="help-group-blurb">
            Workflows from this project and its installed plugins.
          </p>
          <div className="help-commands">
            {skills.map((s) => (
              <button className="help-command" key={s.name} onClick={() => onRun(s.name)}>
                <code className="help-command-name">{s.name}</code>
                <span className="help-command-desc">{s.description}</span>
              </button>
            ))}
          </div>
        </section>
      )}
    </Drawer>
  );
}

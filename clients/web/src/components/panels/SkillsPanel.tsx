import { useEffect, useState } from "react";
import type { EmberClient } from "../../protocol/client";
import { Drawer } from "./Drawer";

interface SkillRow {
  name?: string;
  description?: string;
}

/** Skill workflows — runnable straight from the panel. */
export function SkillsPanel({
  client,
  onRun,
  onClose,
}: {
  client: EmberClient;
  onRun: (command: string) => void;
  onClose: () => void;
}) {
  const [skills, setSkills] = useState<SkillRow[] | null>(null);

  useEffect(() => {
    client
      .rpc<SkillRow[]>("get_skill_details")
      .then((s) => setSkills(s || []))
      .catch(() => setSkills([]));
  }, [client]);

  return (
    <Drawer title="Skills" onClose={onClose}>
      {skills === null && <div className="msg-info">Loading…</div>}
      {skills?.length === 0 && (
        <div className="msg-info">
          No skills installed. Add some via Plugins or .igni/skills/.
        </div>
      )}
      {skills?.map((s) => (
        <div className="row" key={s.name}>
          <div>
            <div className="name">/{s.name}</div>
            <div className="meta">{s.description}</div>
          </div>
          <button
            className="btn btn-sm"
            onClick={() => {
              onClose();
              onRun(`/${s.name}`);
            }}
          >
            Run
          </button>
        </div>
      ))}
    </Drawer>
  );
}

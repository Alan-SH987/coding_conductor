"use client";

import { useState } from "react";
import { api, type Skill } from "@/lib/api";

// A compact per-project skill toggle. Lists globally-installed skills and lets
// you enable/disable them for this project; enabled skills are injected into the
// agent's system prompt (shared across Claude and Codex).
export function SkillsControl({
  projectId,
  enabled,
  onChanged,
}: {
  projectId: number;
  enabled: string[];
  onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [installed, setInstalled] = useState<Skill[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function toggleOpen() {
    const next = !open;
    setOpen(next);
    if (next && installed === null) {
      try {
        setInstalled(await api.listSkills());
      } catch (e) {
        setError(String(e));
      }
    }
  }

  async function toggleSkill(name: string) {
    const next = enabled.includes(name)
      ? enabled.filter((n) => n !== name)
      : [...enabled, name];
    setBusy(true);
    setError(null);
    try {
      await api.updateProjectSkills(projectId, next);
      onChanged();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="relative text-xs">
      <button
        type="button"
        onClick={toggleOpen}
        title="Skills injected into this project's agent runs"
        className="rounded-md border border-border px-2 py-1 text-muted-foreground hover:border-border hover:text-foreground"
      >
        skills{enabled.length ? ` (${enabled.length})` : ""}
      </button>
      {open && (
        <div className="absolute right-0 z-30 mt-1 w-72 rounded-md border border-border bg-background p-2 shadow-xl">
          {error && <p className="px-1 pb-1 text-red-400">{error}</p>}
          {installed === null ? (
            <p className="px-1 text-muted-foreground">loading…</p>
          ) : installed.length === 0 ? (
            <p className="px-1 text-muted-foreground">
              No skills installed. Drop a folder into ~/.conductor/skills/.
            </p>
          ) : (
            <ul className="space-y-1">
              {installed.map((s) => (
                <li key={s.name}>
                  <label className="flex cursor-pointer items-start gap-2 rounded px-1 py-1 hover:bg-card">
                    <input
                      type="checkbox"
                      checked={enabled.includes(s.name)}
                      onChange={() => toggleSkill(s.name)}
                      disabled={busy}
                      className="mt-0.5"
                    />
                    <span>
                      <span className="text-foreground">{s.name}</span>
                      {s.description && (
                        <span className="block text-[10px] text-muted-foreground">
                          {s.description}
                        </span>
                      )}
                    </span>
                  </label>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

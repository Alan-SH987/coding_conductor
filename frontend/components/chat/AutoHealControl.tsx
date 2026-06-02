"use client";

import { useState } from "react";
import { api } from "@/lib/api";

// Compact per-project toggle for the auto self-heal loop: after a successful run,
// the agent self-reviews its diff and auto-revises up to N times before the human
// gate (0 = off, opt-in). Click to cycle 0 → 1 → 2 → 3.
export function AutoHealControl({
  projectId,
  rounds,
  onChanged,
}: {
  projectId: number;
  rounds: number;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);

  async function cycle() {
    setBusy(true);
    try {
      await api.updateProjectAutoHeal(projectId, (rounds + 1) % 4); // 0,1,2,3
      onChanged();
    } finally {
      setBusy(false);
    }
  }

  const on = rounds > 0;
  return (
    <button
      type="button"
      onClick={cycle}
      disabled={busy}
      title="Auto self-heal: after a run, the agent self-reviews and revises its own diff up to N times before the human gate (0 = off). Click to change."
      className={
        "rounded-md border border-border px-2 py-1 text-xs disabled:opacity-50 " +
        (on
          ? "bg-secondary text-secondary-foreground"
          : "text-muted-foreground hover:text-foreground")
      }
    >
      self-heal{on ? ` ${rounds}×` : " off"}
    </button>
  );
}

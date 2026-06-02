"use client";

import { useState } from "react";
import { api } from "@/lib/api";

// Manually trigger memory distillation: summarize this project's accumulated task
// handoffs into a few durable, high-level insights (written to insights.md, kept
// separate from the human-curated global.md). Runs an LLM, so it's on-demand.
export function DistillButton({ projectId }: { projectId: number }) {
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(false);
  const [insights, setInsights] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function distill() {
    setBusy(true);
    setError(null);
    try {
      const res = await api.distillProject(projectId);
      setInsights(res.insights || "");
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
      setOpen(true);
    }
  }

  return (
    <div className="relative text-xs">
      <button
        type="button"
        onClick={distill}
        disabled={busy}
        title="Distill accumulated task handoffs into high-level insights (insights.md)"
        className="rounded-md border border-border px-2 py-1 text-muted-foreground hover:border-border hover:text-foreground disabled:opacity-50"
      >
        {busy ? "distilling…" : "distill memory"}
      </button>
      {open && (
        <div className="absolute right-0 z-30 mt-1 w-80 rounded-md border border-border bg-background p-2 shadow-xl">
          <div className="mb-1 flex items-center justify-between">
            <span className="text-foreground">Distilled insights</span>
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="text-muted-foreground hover:text-foreground"
            >
              ✕
            </button>
          </div>
          {error ? (
            <p className="text-red-400">{error}</p>
          ) : insights ? (
            <pre className="max-h-60 overflow-auto whitespace-pre-wrap text-[11px] text-foreground">
              {insights}
            </pre>
          ) : (
            <p className="text-muted-foreground">
              No handoffs to distill yet — merge or reject a task first.
            </p>
          )}
        </div>
      )}
    </div>
  );
}

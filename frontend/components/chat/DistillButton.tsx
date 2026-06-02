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
        className="rounded-md border border-zinc-800 px-2 py-1 text-zinc-400 hover:border-zinc-700 hover:text-zinc-200 disabled:opacity-50"
      >
        {busy ? "distilling…" : "distill memory"}
      </button>
      {open && (
        <div className="absolute right-0 z-30 mt-1 w-80 rounded-md border border-zinc-800 bg-zinc-950 p-2 shadow-xl">
          <div className="mb-1 flex items-center justify-between">
            <span className="text-zinc-300">Distilled insights</span>
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="text-zinc-500 hover:text-zinc-300"
            >
              ✕
            </button>
          </div>
          {error ? (
            <p className="text-red-400">{error}</p>
          ) : insights ? (
            <pre className="max-h-60 overflow-auto whitespace-pre-wrap text-[11px] text-zinc-300">
              {insights}
            </pre>
          ) : (
            <p className="text-zinc-500">
              No handoffs to distill yet — merge or reject a task first.
            </p>
          )}
        </div>
      )}
    </div>
  );
}

"use client";

import { useState } from "react";
import { api, type AgentHealth, type AgentStatus } from "@/lib/api";

const DOT: Record<AgentStatus, string> = {
  available: "bg-green-400",
  unauthenticated: "bg-amber-400",
  rate_limited: "bg-orange-400",
  unavailable: "bg-red-500",
};

const LABEL: Record<AgentStatus, string> = {
  available: "available",
  unauthenticated: "not authenticated",
  rate_limited: "rate-limited",
  unavailable: "unavailable",
};

function agentTitle(a: AgentHealth): string {
  const parts = [`${a.name}: ${LABEL[a.status]}`];
  if (a.version) parts.push(`(${a.version})`);
  if (a.detail && a.detail !== "ok") parts.push(`— ${a.detail}`);
  return parts.join(" ");
}

// A manual status light for each AI CLI. Probing actually runs the CLIs and
// spends a tiny bit of quota, so it only fires on click — never on page load.
export function AgentsHealth() {
  const [data, setData] = useState<AgentHealth[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function check() {
    setLoading(true);
    setError(null);
    try {
      setData(await api.checkAgentsHealth());
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="flex items-center gap-2 text-xs">
      {data?.map((a) => (
        <span key={a.name} title={agentTitle(a)} className="flex items-center gap-1">
          <span className={`inline-block h-2 w-2 rounded-full ${DOT[a.status]}`} />
          <span className="text-zinc-300">{a.name}</span>
        </span>
      ))}
      {error && <span className="max-w-[12rem] truncate text-red-400" title={error}>{error}</span>}
      <button
        type="button"
        onClick={check}
        disabled={loading}
        title="Probe each AI CLI now (spends a tiny bit of quota each time)"
        className="rounded-md border border-zinc-800 px-2 py-1 text-zinc-400 hover:border-zinc-700 hover:text-zinc-200 disabled:opacity-50"
      >
        {loading ? "checking…" : data ? "↻ agents" : "check agents"}
      </button>
    </div>
  );
}

"use client";

import { useState, type ReactNode } from "react";

// A collapsed-by-default card that lazy-loads its body the first time it's
// opened, then caches it. `loader` runs once; `children` renders the result
// (which may legitimately be null, e.g. "no review yet").
export function Collapsible<T>({
  title,
  right,
  loader,
  children,
}: {
  title: string;
  right?: ReactNode;
  loader: () => Promise<T>;
  children: (data: T) => ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [data, setData] = useState<T | null>(null);

  async function toggle() {
    const next = !open;
    setOpen(next);
    if (next && !loaded && !loading) {
      setLoading(true);
      setError(null);
      try {
        setData(await loader());
        setLoaded(true);
      } catch (e) {
        setError(String(e));
      } finally {
        setLoading(false);
      }
    }
  }

  return (
    <div className="rounded-md border border-zinc-800 bg-zinc-900/40">
      <button
        type="button"
        onClick={toggle}
        className="flex w-full items-center justify-between gap-2 px-3 py-1.5 text-left text-xs font-medium text-zinc-300 hover:bg-zinc-800/40"
      >
        <span className="flex items-center gap-2">
          <span className="text-zinc-600">{open ? "▾" : "▸"}</span>
          {title}
        </span>
        {right}
      </button>
      {open && (
        <div className="border-t border-zinc-800 px-3 py-2">
          {loading && <div className="text-xs text-zinc-500">loading…</div>}
          {error && <div className="text-xs text-red-400">{error}</div>}
          {!loading && !error && loaded && children(data as T)}
        </div>
      )}
    </div>
  );
}

// Render a unified diff with light per-line coloring. No syntax-highlighting
// dependency — just prefix-based classes, which reads well for diffs.
export function DiffView({ diff }: { diff: string }) {
  if (!diff.trim()) {
    return <div className="text-xs text-zinc-500">No diff.</div>;
  }
  return (
    <pre className="overflow-x-auto text-xs leading-relaxed">
      {diff.split("\n").map((line, i) => (
        <div key={i} className={diffLineClass(line)}>
          {line || " "}
        </div>
      ))}
    </pre>
  );
}

function diffLineClass(line: string): string {
  if (line.startsWith("+++") || line.startsWith("---")) return "text-zinc-500";
  if (line.startsWith("@@")) return "text-cyan-300";
  if (line.startsWith("diff ") || line.startsWith("index ")) return "text-zinc-600";
  if (line.startsWith("+")) return "text-green-300";
  if (line.startsWith("-")) return "text-red-300";
  return "text-zinc-400";
}

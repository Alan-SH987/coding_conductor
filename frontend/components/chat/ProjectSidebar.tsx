"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { api, type Project } from "@/lib/api";
import { Button } from "@/components/ui";

export function ProjectSidebar({
  projects,
  currentId,
  onChanged,
}: {
  projects: Project[];
  currentId: number;
  onChanged: () => void;
}) {
  const router = useRouter();
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("");
  const [path, setPath] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onCreate() {
    const n = name.trim();
    const p = path.trim();
    if (!n || !p || busy) return;
    setBusy(true);
    setError(null);
    try {
      const created = await api.createProject(n, p);
      setName("");
      setPath("");
      setAdding(false);
      router.push(`/projects/${created.id}`);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function onDelete(p: Project) {
    if (
      !confirm(
        `Delete project "${p.name}"? Its tasks are removed too. (The git repo on disk is left untouched.)`,
      )
    ) {
      return;
    }
    setError(null);
    try {
      await api.deleteProject(p.id);
      if (p.id === currentId) {
        const rest = projects.filter((x) => x.id !== p.id);
        router.push(rest.length ? `/projects/${rest[0].id}` : "/");
      } else {
        onChanged();
      }
    } catch (e) {
      setError(String(e));
    }
  }

  return (
    <aside className="flex h-full w-56 shrink-0 flex-col rounded-lg border border-border bg-card/30">
      <div className="flex items-center justify-between border-b border-border px-3 py-2">
        <span className="text-xs font-semibold text-muted-foreground">Projects</span>
        <button
          type="button"
          onClick={() => setAdding((v) => !v)}
          title="New project"
          className="rounded px-1.5 text-sm text-muted-foreground hover:bg-muted hover:text-foreground"
        >
          {adding ? "×" : "+"}
        </button>
      </div>

      {adding && (
        <div className="space-y-1.5 border-b border-border p-2">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="name"
            autoFocus
            className="w-full rounded-md border border-border bg-card px-2 py-1 text-sm outline-none focus:border-ring"
          />
          <input
            value={path}
            onChange={(e) => setPath(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") onCreate();
              if (e.key === "Escape") setAdding(false);
            }}
            placeholder="/absolute/path"
            className="w-full rounded-md border border-border bg-card px-2 py-1 font-mono text-[11px] outline-none focus:border-ring"
          />
          {error && <p className="text-[10px] text-red-400">{error}</p>}
          <div className="flex items-center gap-1.5">
            <Button
              onClick={onCreate}
              disabled={busy || !name.trim() || !path.trim()}
              className="px-2 py-1 text-xs"
            >
              {busy ? "Creating…" : "Create"}
            </Button>
            <button
              type="button"
              onClick={() => setAdding(false)}
              className="text-xs text-muted-foreground hover:text-foreground"
            >
              Cancel
            </button>
          </div>
          <p className="text-[10px] text-muted-foreground">
            An empty or non-git path is auto-initialized with git.
          </p>
        </div>
      )}

      <nav className="flex-1 space-y-1 overflow-y-auto p-2">
        {projects.length === 0 ? (
          <p className="px-2 py-1 text-xs text-muted-foreground">No projects.</p>
        ) : (
          projects.map((p) => {
            const active = p.id === currentId;
            return (
              <div
                key={p.id}
                className={`group relative rounded-md ${
                  active ? "bg-muted" : "hover:bg-card"
                }`}
              >
                <Link
                  href={`/projects/${p.id}`}
                  className={`block rounded-md px-3 py-2 pr-7 ${
                    active ? "text-foreground" : "text-muted-foreground hover:text-foreground"
                  }`}
                >
                  <div className="flex items-center gap-1.5 text-sm">
                    {p.is_pinned && <span className="text-amber-400">★</span>}
                    <span className="truncate">{p.name}</span>
                  </div>
                  <div className="truncate font-mono text-[10px] text-muted-foreground">
                    {p.path}
                  </div>
                </Link>
                <button
                  type="button"
                  onClick={() => onDelete(p)}
                  title="Delete project"
                  className="absolute right-1 top-1.5 hidden rounded p-1 text-muted-foreground hover:bg-secondary hover:text-red-300 group-hover:block"
                >
                  ✕
                </button>
              </div>
            );
          })
        )}
      </nav>
    </aside>
  );
}

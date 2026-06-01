"use client";

import Link from "next/link";
import type { Project } from "@/lib/api";

export function ProjectSidebar({
  projects,
  currentId,
}: {
  projects: Project[];
  currentId: number;
}) {
  return (
    <aside className="flex h-full w-56 shrink-0 flex-col rounded-lg border border-zinc-800 bg-zinc-900/30">
      <div className="border-b border-zinc-800 px-3 py-2 text-xs font-semibold text-zinc-400">
        Projects
      </div>
      <nav className="flex-1 space-y-1 overflow-y-auto p-2">
        {projects.length === 0 ? (
          <p className="px-2 py-1 text-xs text-zinc-500">No projects.</p>
        ) : (
          projects.map((p) => {
            const active = p.id === currentId;
            return (
              <Link
                key={p.id}
                href={`/projects/${p.id}`}
                className={`block rounded-md px-3 py-2 ${
                  active
                    ? "bg-zinc-800 text-zinc-100"
                    : "text-zinc-400 hover:bg-zinc-900 hover:text-zinc-200"
                }`}
              >
                <div className="flex items-center gap-1.5 text-sm">
                  {p.is_pinned && <span className="text-amber-400">★</span>}
                  <span className="truncate">{p.name}</span>
                </div>
                <div className="truncate font-mono text-[10px] text-zinc-600">
                  {p.path}
                </div>
              </Link>
            );
          })
        )}
      </nav>
    </aside>
  );
}

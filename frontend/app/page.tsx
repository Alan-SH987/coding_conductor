"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { api, type Project } from "@/lib/api";
import { Button, Card, Input } from "@/components/ui";

export default function HomePage() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [name, setName] = useState("");
  const [path, setPath] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [showArchived, setShowArchived] = useState(false);

  async function load() {
    try {
      setProjects(await api.listProjects(showArchived));
    } catch (e) {
      setError(String(e));
    }
  }

  useEffect(() => {
    load();
  }, [showArchived]);

  async function onCreate(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await api.createProject(name.trim(), path.trim());
      setName("");
      setPath("");
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function handlePin(p: Project, e: React.MouseEvent) {
    e.preventDefault();
    e.stopPropagation();
    try {
      if (p.is_pinned) {
        await api.unpinProject(p.id);
      } else {
        await api.pinProject(p.id);
      }
      await load();
    } catch (e) {
      setError(String(e));
    }
  }

  async function handleArchive(p: Project, e: React.MouseEvent) {
    e.preventDefault();
    e.stopPropagation();
    try {
      if (p.is_archived) {
        await api.unarchiveProject(p.id);
      } else {
        await api.archiveProject(p.id);
      }
      await load();
    } catch (e) {
      setError(String(e));
    }
  }

  async function handleDelete(p: Project, e: React.MouseEvent) {
    e.preventDefault();
    e.stopPropagation();
    if (!confirm(`Delete project "${p.name}"? This action cannot be undone.`)) {
      return;
    }
    try {
      await api.deleteProject(p.id);
      await load();
    } catch (e) {
      setError(String(e));
    }
  }

  return (
    <div className="space-y-8">
      <section>
        <div className="mb-4 flex items-center justify-between">
          <h1 className="text-xl font-semibold">Projects</h1>
          <label className="flex items-center gap-2 text-sm text-zinc-400">
            <input
              type="checkbox"
              checked={showArchived}
              onChange={(e) => setShowArchived(e.target.checked)}
              className="rounded border-zinc-600"
            />
            Show archived
          </label>
        </div>
        {projects.length === 0 ? (
          <p className="text-sm text-zinc-500">No projects yet.</p>
        ) : (
          <div className="space-y-2">
            {projects.map((p) => (
              <Link key={p.id} href={`/projects/${p.id}`} className="block">
                <Card
                  className={`hover:border-zinc-600 ${
                    p.is_archived ? "opacity-60" : ""
                  }`}
                >
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      {p.is_pinned && (
                        <span className="text-yellow-500" title="Pinned">
                          ★
                        </span>
                      )}
                      <span className="font-medium">{p.name}</span>
                      {p.is_archived && (
                        <span className="rounded bg-zinc-700 px-1.5 py-0.5 text-xs text-zinc-400">
                          Archived
                        </span>
                      )}
                    </div>
                    <div className="flex items-center gap-2">
                      <span className="text-xs text-zinc-500">
                        {p.default_branch}
                      </span>
                      <div className="flex gap-1">
                        <button
                          onClick={(e) => handlePin(p, e)}
                          className={`rounded p-1 text-xs hover:bg-zinc-700 ${
                            p.is_pinned
                              ? "text-yellow-500"
                              : "text-zinc-500 hover:text-yellow-500"
                          }`}
                          title={p.is_pinned ? "Unpin" : "Pin"}
                        >
                          {p.is_pinned ? "★" : "☆"}
                        </button>
                        <button
                          onClick={(e) => handleArchive(p, e)}
                          className="rounded p-1 text-xs text-zinc-500 hover:bg-zinc-700 hover:text-zinc-300"
                          title={p.is_archived ? "Unarchive" : "Archive"}
                        >
                          {p.is_archived ? "↩" : "📦"}
                        </button>
                        <button
                          onClick={(e) => handleDelete(p, e)}
                          className="rounded p-1 text-xs text-zinc-500 hover:bg-red-900 hover:text-red-300"
                          title="Delete"
                        >
                          🗑
                        </button>
                      </div>
                    </div>
                  </div>
                  <div className="mt-1 truncate font-mono text-xs text-zinc-500">
                    {p.path}
                  </div>
                </Card>
              </Link>
            ))}
          </div>
        )}
      </section>

      <section>
        <h2 className="mb-3 text-sm font-semibold text-zinc-400">
          Add a project
        </h2>
        <Card>
          <form onSubmit={onCreate} className="space-y-3">
            <Input
              placeholder="name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              required
            />
            <Input
              placeholder="absolute path to a git repo"
              value={path}
              onChange={(e) => setPath(e.target.value)}
              required
            />
            <Button type="submit" disabled={busy || !name || !path}>
              {busy ? "Creating…" : "Create project"}
            </Button>
          </form>
        </Card>
      </section>

      {error && (
        <p className="text-sm text-red-400">{error}</p>
      )}
    </div>
  );
}

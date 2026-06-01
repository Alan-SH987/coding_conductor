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

  async function load() {
    try {
      setProjects(await api.listProjects());
    } catch (e) {
      setError(String(e));
    }
  }

  useEffect(() => {
    load();
  }, []);

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

  return (
    <div className="space-y-8">
      <section>
        <h1 className="mb-4 text-xl font-semibold">Projects</h1>
        {projects.length === 0 ? (
          <p className="text-sm text-zinc-500">No projects yet.</p>
        ) : (
          <div className="space-y-2">
            {projects.map((p) => (
              <Link key={p.id} href={`/projects/${p.id}`} className="block">
                <Card className="hover:border-zinc-600">
                  <div className="flex items-center justify-between">
                    <span className="font-medium">{p.name}</span>
                    <span className="text-xs text-zinc-500">
                      {p.default_branch}
                    </span>
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

"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import {
  api,
  type Agent,
  type Project,
  type Task,
} from "@/lib/api";
import { Badge, Button, Card, Input } from "@/components/ui";

export default function ProjectPage({
  params,
}: {
  params: { id: string };
}) {
  const projectId = Number(params.id);
  const [project, setProject] = useState<Project | null>(null);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [agent, setAgent] = useState("claude");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function load() {
    try {
      const [p, t, a] = await Promise.all([
        api.getProject(projectId),
        api.listTasks(projectId),
        api.listAgents(),
      ]);
      setProject(p);
      setTasks(t);
      setAgents(a);
      if (a.length && !a.some((x) => x.name === agent)) setAgent(a[0].name);
    } catch (e) {
      setError(String(e));
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  async function onCreate(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await api.createTask(projectId, title.trim(), description.trim(), agent);
      setTitle("");
      setDescription("");
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  const topLevel = tasks.filter((t) => t.parent_id == null);
  const childCount = (id: number) =>
    tasks.filter((t) => t.parent_id === id).length;

  return (
    <div className="space-y-8">
      <div>
        <Link href="/" className="text-xs text-zinc-500 hover:text-zinc-300">
          ← projects
        </Link>
        <h1 className="mt-2 text-xl font-semibold">
          {project?.name ?? `Project ${projectId}`}
        </h1>
        {project && (
          <div className="mt-1 truncate font-mono text-xs text-zinc-500">
            {project.path}
          </div>
        )}
      </div>

      <section>
        <h2 className="mb-3 text-sm font-semibold text-zinc-400">Tasks</h2>
        {topLevel.length === 0 ? (
          <p className="text-sm text-zinc-500">No tasks yet.</p>
        ) : (
          <div className="space-y-2">
            {topLevel.map((t) => (
              <Link key={t.id} href={`/tasks/${t.id}`} className="block">
                <Card className="hover:border-zinc-600">
                  <div className="flex items-center justify-between gap-3">
                    <span className="truncate font-medium">{t.title}</span>
                    <Badge status={t.status} />
                  </div>
                  <div className="mt-1 text-xs text-zinc-500">
                    agent: {t.assigned_agent ?? "—"}
                    {t.branch ? ` · ${t.branch}` : ""}
                    {childCount(t.id) > 0 ? ` · ${childCount(t.id)} subtasks` : ""}
                  </div>
                </Card>
              </Link>
            ))}
          </div>
        )}
      </section>

      <section>
        <h2 className="mb-3 text-sm font-semibold text-zinc-400">New task</h2>
        <Card>
          <form onSubmit={onCreate} className="space-y-3">
            <Input
              placeholder="title"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              required
            />
            <textarea
              placeholder="description (optional)"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={3}
              className="w-full rounded-md border border-zinc-700 bg-zinc-900 px-3 py-1.5 text-sm outline-none focus:border-zinc-500"
            />
            <div className="flex items-center gap-3">
              <select
                value={agent}
                onChange={(e) => setAgent(e.target.value)}
                className="rounded-md border border-zinc-700 bg-zinc-900 px-3 py-1.5 text-sm outline-none focus:border-zinc-500"
              >
                {agents.map((a) => (
                  <option key={a.name} value={a.name}>
                    {a.name}
                  </option>
                ))}
              </select>
              <Button type="submit" disabled={busy || !title}>
                {busy ? "Creating…" : "Create task"}
              </Button>
            </div>
          </form>
        </Card>
      </section>

      {error && <p className="text-sm text-red-400">{error}</p>}
    </div>
  );
}

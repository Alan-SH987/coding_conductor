"use client";

import { useEffect, useRef, useState } from "react";
import {
  api,
  type Agent,
  type Event as ApiEvent,
  type Project,
  type Task,
} from "@/lib/api";
import { Button } from "@/components/ui";
import { ProjectSidebar } from "@/components/chat/ProjectSidebar";
import { ConversationTurn } from "@/components/chat/ConversationTurn";

export default function ProjectPage({
  params,
}: {
  params: { id: string };
}) {
  const projectId = Number(params.id);

  const [projects, setProjects] = useState<Project[]>([]);
  const [project, setProject] = useState<Project | null>(null);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [agent, setAgent] = useState("claude");
  const [message, setMessage] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [busyTaskId, setBusyTaskId] = useState<number | null>(null);
  const [streamingTaskId, setStreamingTaskId] = useState<number | null>(null);
  const [liveEvents, setLiveEvents] = useState<ApiEvent[]>([]);
  // Bumped on every reload so conversation turns remount and re-fetch their
  // collapsibles (otherwise a lazy card would cache stale diff/review data).
  const [reloadNonce, setReloadNonce] = useState(0);

  const esRef = useRef<EventSource | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  async function load(): Promise<Task[]> {
    const [ps, p, t, a] = await Promise.all([
      api.listProjects(),
      api.getProject(projectId),
      api.listTasks(projectId),
      api.listAgents(),
    ]);
    setProjects(ps);
    setProject(p);
    setTasks([...t].sort((x, y) => x.id - y.id));
    setAgents(a);
    setReloadNonce((n) => n + 1);
    return t;
  }

  // Tail the latest run of `taskId`. The backend replays from seq 0, so a fresh
  // run and a resume-on-mount are the same path. On "done", reload to pick up
  // the new status / runs / diff / review.
  function openStream(taskId: number) {
    esRef.current?.close();
    setStreamingTaskId(taskId);
    setLiveEvents([]);
    const es = new EventSource(api.streamUrl(taskId));
    esRef.current = es;
    let finished = false;

    es.addEventListener("ev", (e) => {
      const ev = JSON.parse((e as MessageEvent).data) as ApiEvent;
      setLiveEvents((prev) =>
        prev.some((p) => p.id === ev.id) ? prev : [...prev, ev],
      );
    });

    es.addEventListener("done", async () => {
      finished = true;
      es.close();
      if (esRef.current === es) esRef.current = null;
      setStreamingTaskId(null);
      setLiveEvents([]);
      try {
        await load();
      } catch (err) {
        setError(String(err));
      }
    });

    es.onerror = async () => {
      if (finished) return; // normal server close, handled by "done"
      es.close();
      if (esRef.current === es) esRef.current = null;
      setError("stream disconnected");
      setStreamingTaskId(null);
      setLiveEvents([]);
      try {
        await load();
      } catch {
        // already surfaced
      }
    };
  }

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const t = await load();
        if (cancelled) return;
        const running = t.find((x) => x.status === "running");
        if (running) openStream(running.id);
      } catch (e) {
        if (!cancelled) setError(String(e));
      }
    })();
    return () => {
      cancelled = true;
      esRef.current?.close();
      esRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  // Default the agent picker to a real adapter (the "/agents" list includes a
  // synthetic "auto" that create_task rejects).
  useEffect(() => {
    const real = agents.filter((a) => a.name !== "auto");
    if (real.length && !real.some((a) => a.name === agent)) {
      setAgent(real[0].name);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agents]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [tasks.length, liveEvents.length, streamingTaskId]);

  async function onSend() {
    const title = message.trim();
    if (!title || sending) return;
    setError(null);
    setSending(true);
    try {
      const task = await api.createTask(projectId, title, "", agent);
      setMessage("");
      await load();
      await api.runTask(task.id);
      openStream(task.id);
    } catch (e) {
      setError(String(e));
    } finally {
      setSending(false);
    }
  }

  async function onRun(id: number) {
    setError(null);
    setBusyTaskId(id);
    try {
      await api.runTask(id);
      openStream(id);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusyTaskId(null);
    }
  }

  async function onApprove(id: number) {
    setError(null);
    setBusyTaskId(id);
    try {
      const res = await api.approve(id);
      if (!res.ok && res.conflict) {
        setError(`merge conflict: ${res.conflicted_files.join(", ")}`);
      }
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusyTaskId(null);
    }
  }

  async function onReject(id: number) {
    setError(null);
    setBusyTaskId(id);
    try {
      await api.reject(id);
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusyTaskId(null);
    }
  }

  async function onReview(id: number) {
    setError(null);
    setBusyTaskId(id);
    try {
      await api.reviewTask(id);
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusyTaskId(null);
    }
  }

  const childParentIds = new Set(
    tasks.filter((t) => t.parent_id != null).map((t) => t.parent_id as number),
  );
  const realAgents = agents.filter((a) => a.name !== "auto");

  return (
    <div className="flex h-[calc(100vh-9rem)] gap-4">
      <ProjectSidebar projects={projects} currentId={projectId} />

      <div className="flex flex-1 flex-col overflow-hidden">
        <div className="mb-2 flex items-center gap-3">
          <h1 className="truncate text-lg font-semibold">
            {project?.name ?? `Project ${projectId}`}
          </h1>
        </div>

        <div className="flex-1 space-y-4 overflow-y-auto rounded-lg border border-zinc-800 bg-zinc-950/40 p-4">
          {tasks.length === 0 ? (
            <p className="text-sm text-zinc-500">
              No tasks yet. Send a message below to create one.
            </p>
          ) : (
            tasks.map((t) => (
              <ConversationTurn
                key={`${t.id}:${reloadNonce}`}
                task={t}
                hasChildren={childParentIds.has(t.id)}
                streaming={streamingTaskId === t.id}
                liveEvents={streamingTaskId === t.id ? liveEvents : []}
                busy={busyTaskId === t.id}
                onRun={onRun}
                onReview={onReview}
                onApprove={onApprove}
                onReject={onReject}
              />
            ))
          )}
          <div ref={bottomRef} />
        </div>

        {error && <p className="mt-2 text-sm text-red-400">{error}</p>}

        <div className="mt-2 flex items-end gap-2">
          <textarea
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                onSend();
              }
            }}
            rows={2}
            placeholder="Describe a task…  (Enter to send, Shift+Enter for newline)"
            className="flex-1 resize-none rounded-md border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm outline-none focus:border-zinc-500"
          />
          <select
            value={agent}
            onChange={(e) => setAgent(e.target.value)}
            className="rounded-md border border-zinc-700 bg-zinc-900 px-2 py-2 text-sm outline-none focus:border-zinc-500"
          >
            {realAgents.map((a) => (
              <option key={a.name} value={a.name}>
                {a.name}
              </option>
            ))}
          </select>
          <Button onClick={onSend} disabled={sending || !message.trim()} className="px-4">
            {sending ? "Sending…" : "Send"}
          </Button>
        </div>
      </div>
    </div>
  );
}

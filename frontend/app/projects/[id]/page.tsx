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
import {
  RightPanel,
  type PanelKind,
  type PanelState,
} from "@/components/chat/RightPanel";

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
  // Which verbose view (if any) is open in the right-side drawer.
  const [panel, setPanel] = useState<PanelState | null>(null);
  // Pre-merge verify gate: output of the last failed approve + the inline editor
  // for the project's verify command.
  const [verifyFail, setVerifyFail] = useState<{ taskId: number; output: string } | null>(null);
  const [editingVerify, setEditingVerify] = useState(false);
  const [verifyDraft, setVerifyDraft] = useState("");
  const [savingVerify, setSavingVerify] = useState(false);
  // Bumped on every reload so the right-side panel re-fetches instead of showing
  // stale diff/review/activity data after a run finishes or a merge lands.
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

  function openPanel(kind: PanelKind, taskId: number) {
    setPanel({ kind, taskId });
  }

  // When a stream ends, a "live" panel has nothing more to show — fold it into
  // the persisted "activity" view for the same task so the drawer stays useful.
  function endLivePanel(taskId: number) {
    setPanel((prev) =>
      prev?.kind === "live" && prev.taskId === taskId
        ? { kind: "activity", taskId }
        : prev,
    );
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
      endLivePanel(taskId);
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
      endLivePanel(taskId);
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
        if (running) {
          openStream(running.id);
          openPanel("live", running.id);
        }
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
      openPanel("live", task.id);
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
      openPanel("live", id);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusyTaskId(null);
    }
  }

  async function onApprove(id: number) {
    setError(null);
    setVerifyFail(null);
    setBusyTaskId(id);
    try {
      const res = await api.approve(id);
      if (!res.ok) {
        if (res.conflict) {
          setError(`merge conflict: ${res.conflicted_files.join(", ")}`);
        } else if (res.verify_failed) {
          // Gate blocked it: the merge was aborted, main is untouched, and the
          // task stays at awaiting_approval so it can be revised and retried.
          setVerifyFail({ taskId: id, output: res.verify_output });
          setError("verify failed — merge aborted, nothing landed on main.");
        } else {
          setError(res.verify_output || "merge failed");
        }
      }
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusyTaskId(null);
    }
  }

  async function saveVerify() {
    setSavingVerify(true);
    setError(null);
    try {
      const cmd = verifyDraft.trim();
      const updated = await api.updateProjectVerify(projectId, cmd || null);
      setProject(updated);
      setEditingVerify(false);
    } catch (e) {
      setError(String(e));
    } finally {
      setSavingVerify(false);
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
      openPanel("review", id);
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

      <div className="relative flex flex-1 overflow-hidden">
        <div className="flex flex-1 flex-col overflow-hidden">
          <div className="mb-2 flex items-center gap-3">
            <h1 className="truncate text-lg font-semibold">
              {project?.name ?? `Project ${projectId}`}
            </h1>
            <div className="ml-auto flex items-center gap-2 text-xs">
              {editingVerify ? (
                <>
                  <input
                    value={verifyDraft}
                    onChange={(e) => setVerifyDraft(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") saveVerify();
                      if (e.key === "Escape") setEditingVerify(false);
                    }}
                    placeholder="e.g. cd frontend && npm run build"
                    className="w-80 rounded-md border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono outline-none focus:border-zinc-500"
                    autoFocus
                  />
                  <Button onClick={saveVerify} disabled={savingVerify} className="px-2 py-1">
                    {savingVerify ? "Saving…" : "Save"}
                  </Button>
                  <button
                    type="button"
                    onClick={() => setEditingVerify(false)}
                    className="text-zinc-500 hover:text-zinc-300"
                  >
                    Cancel
                  </button>
                </>
              ) : (
                <button
                  type="button"
                  onClick={() => {
                    setVerifyDraft(project?.verify_cmd ?? "");
                    setEditingVerify(true);
                  }}
                  title="Command run against the merged result before a task lands on main. Click to edit."
                  className="flex max-w-md items-center gap-1.5 rounded-md border border-zinc-800 px-2 py-1 hover:border-zinc-700"
                >
                  <span className="text-zinc-500">verify gate:</span>
                  <span className="truncate font-mono text-zinc-300">
                    {project?.verify_cmd ? project.verify_cmd : "off"}
                  </span>
                </button>
              )}
            </div>
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
                  busy={busyTaskId === t.id}
                  activeKind={panel?.taskId === t.id ? panel.kind : null}
                  onRun={onRun}
                  onReview={onReview}
                  onApprove={onApprove}
                  onReject={onReject}
                  onOpenPanel={openPanel}
                />
              ))
            )}
            <div ref={bottomRef} />
          </div>

          {error && <p className="mt-2 text-sm text-red-400">{error}</p>}

          {verifyFail && (
            <div className="mt-2 rounded-md border border-amber-700/60 bg-amber-950/30 px-3 py-2">
              <div className="mb-1 flex items-center justify-between">
                <span className="text-xs font-medium text-amber-300">
                  Verify failed for #{verifyFail.taskId} — merge aborted, main untouched
                </span>
                <button
                  type="button"
                  onClick={() => setVerifyFail(null)}
                  aria-label="Dismiss"
                  className="text-amber-600 hover:text-amber-300"
                >
                  ✕
                </button>
              </div>
              <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words font-mono text-xs text-zinc-300">
                {verifyFail.output || "(no output)"}
              </pre>
            </div>
          )}

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

        <RightPanel
          panel={panel}
          liveEvents={liveEvents}
          streaming={panel?.kind === "live" && streamingTaskId === panel.taskId}
          reloadNonce={reloadNonce}
          onClose={() => setPanel(null)}
        />
      </div>
    </div>
  );
}

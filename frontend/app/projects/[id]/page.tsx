"use client";

import { useEffect, useRef, useState } from "react";
import {
  api,
  type Agent,
  type Event as ApiEvent,
  type Project,
  type ProjectUsage,
  type Task,
} from "@/lib/api";
import { Button } from "@/components/ui";
import { ProjectSidebar } from "@/components/chat/ProjectSidebar";
import { ConversationTurn } from "@/components/chat/ConversationTurn";
import { AgentsHealth } from "@/components/chat/AgentsHealth";
import { SkillsControl } from "@/components/chat/SkillsControl";
import { DistillButton } from "@/components/chat/DistillButton";
import { AutoHealControl } from "@/components/chat/AutoHealControl";
import {
  RightPanel,
  type PanelKind,
  type PanelState,
} from "@/components/chat/RightPanel";

// First non-empty line of a "thinking" event's payload, trimmed for inline display.
function firstThought(payloadJson: string): string | null {
  try {
    const p = JSON.parse(payloadJson);
    const line = String(p.text || "")
      .split("\n")
      .map((s: string) => s.trim())
      .find(Boolean);
    if (!line) return null;
    return line.length > 140 ? line.slice(0, 137) + "..." : line;
  } catch {
    return null;
  }
}

export default function ProjectPage({
  params,
}: {
  params: { id: string };
}) {
  const projectId = Number(params.id);
  type PendingAttachment = {
    id: string;
    file: File;
    previewUrl: string | null;
  };

  const [projects, setProjects] = useState<Project[]>([]);
  const [project, setProject] = useState<Project | null>(null);
  const [usage, setUsage] = useState<ProjectUsage | null>(null);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [agent, setAgent] = useState("auto");
  const [message, setMessage] = useState("");
  const [attachments, setAttachments] = useState<PendingAttachment[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [busyTaskId, setBusyTaskId] = useState<number | null>(null);
  const [streamingTaskId, setStreamingTaskId] = useState<number | null>(null);
  const [liveEvents, setLiveEvents] = useState<ApiEvent[]>([]);
  // Persist key progress info per task (survives stream end)
  const [taskProgress, setTaskProgress] = useState<
    Record<number, { toolCalls: number; lastTool: string | null; summary: string | null; thoughts?: string[] }>
  >({});
  // Tag filtering state
  const [selectedTags, setSelectedTags] = useState<Set<string>>(new Set());
  // Which verbose view (if any) is open in the right-side drawer.
  const [panel, setPanel] = useState<PanelState | null>(null);
  // Pre-merge verify gate: output of the last failed approve + the inline editor
  // for the project's verify command.
  const [verifyFail, setVerifyFail] = useState<{ taskId: number; output: string } | null>(null);
  const [editingVerify, setEditingVerify] = useState(false);
  const [verifyDraft, setVerifyDraft] = useState("");
  const [savingVerify, setSavingVerify] = useState(false);
  const [editingBudget, setEditingBudget] = useState(false);
  const [quotaTokensDraft, setQuotaTokensDraft] = useState("");
  const [quotaCostDraft, setQuotaCostDraft] = useState("");
  const [savingBudget, setSavingBudget] = useState(false);
  // Bumped on every reload so the right-side panel re-fetches instead of showing
  // stale diff/review/activity data after a run finishes or a merge lands.
  const [reloadNonce, setReloadNonce] = useState(0);
  // Follow-up task creation - uses the main input area instead of a modal
  const [followUpSourceId, setFollowUpSourceId] = useState<number | null>(null);
  const [creatingFollowUp, setCreatingFollowUp] = useState(false);

  const esRef = useRef<EventSource | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const attachmentsRef = useRef<PendingAttachment[]>([]);
  const isInitialLoad = useRef(true);
  const [isDragging, setIsDragging] = useState(false);
  const dragCounterRef = useRef(0);

  // Extract progress info from a list of events (for historical tasks)
  function extractProgressFromEvents(
    events: ApiEvent[],
  ): { toolCalls: number; lastTool: string | null; summary: string | null; thoughts?: string[] } {
    let toolCalls = 0;
    let lastTool: string | null = null;
    let summary: string | null = null;
    const thoughts: string[] = [];

    for (const ev of events) {
      if (ev.type === "tool_use") {
        toolCalls++;
        try {
          const p = JSON.parse(ev.payload_json);
          // payload_json is {"text": ..., "data": {...}}, tool name is in data.name or data.tool
          lastTool = p.data?.name || p.data?.tool || p.tool || p.name || null;
        } catch {
          // ignore
        }
      } else if (ev.type === "thinking") {
        const t = firstThought(ev.payload_json);
        if (t && thoughts[thoughts.length - 1] !== t) thoughts.push(t);
      } else if (ev.type === "final") {
        try {
          const p = JSON.parse(ev.payload_json);
          // payload_json is {"text": ..., "data": {...}}, summary is in text field
          if (p.text) {
            summary = p.text;
          }
        } catch {
          // ignore
        }
      }
    }

    return { toolCalls, lastTool, summary, thoughts: thoughts.slice(-15) };
  }

  async function load(): Promise<Task[]> {
    const [ps, p, u, t, a] = await Promise.all([
      api.listProjects(),
      api.getProject(projectId),
      api.getProjectUsage(projectId),
      api.listTasks(projectId),
      api.listAgents(),
    ]);
    setProjects(ps);
    setProject(p);
    setUsage(u);
    setTasks([...t].sort((x, y) => x.id - y.id));
    setAgents(a);
    setReloadNonce((n) => n + 1);

    // Load historical progress for completed tasks that don't have progress yet
    const completedStatuses = ["awaiting_approval", "merged", "rejected", "failed"];
    const tasksNeedingProgress = t.filter(
      (task) =>
        completedStatuses.includes(task.status) &&
        !taskProgress[task.id],
    );

    if (tasksNeedingProgress.length > 0) {
      // Fetch runs and events for tasks without progress info
      // Limit concurrent requests to avoid overwhelming the server
      const progressPromises = tasksNeedingProgress.slice(0, 20).map(async (task) => {
        try {
          const runs = await api.listRuns(task.id);
          if (runs.length === 0) return null;
          // Get the most recent run
          const latestRun = runs.reduce((a, b) => (a.id > b.id ? a : b));
          const events = await api.listEvents(latestRun.id);
          const progress = extractProgressFromEvents(events);
          return { taskId: task.id, progress };
        } catch {
          return null;
        }
      });

      const results = await Promise.all(progressPromises);
      const newProgress: Record<number, { toolCalls: number; lastTool: string | null; summary: string | null; thoughts?: string[] }> = {};
      for (const result of results) {
        // Include tasks that have tool calls OR a summary
        if (result && (result.progress.toolCalls > 0 || result.progress.summary)) {
          newProgress[result.taskId] = result.progress;
        }
      }

      if (Object.keys(newProgress).length > 0) {
        setTaskProgress((prev) => ({ ...prev, ...newProgress }));
      }
    }

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
      // Update persisted progress for this task
      if (ev.type === "tool_use") {
        try {
          const p = JSON.parse(ev.payload_json);
          // payload_json is {"text": ..., "data": {...}}, tool name is in data.name or data.tool
          const tool = p.data?.name || p.data?.tool || p.tool || p.name || null;
          setTaskProgress((prev) => ({
            ...prev,
            [taskId]: {
              ...prev[taskId],
              toolCalls: (prev[taskId]?.toolCalls || 0) + 1,
              lastTool: tool,
            },
          }));
        } catch {
          // ignore parse errors
        }
      } else if (ev.type === "thinking") {
        const t = firstThought(ev.payload_json);
        if (t) {
          setTaskProgress((prev) => {
            const cur = prev[taskId];
            const thoughts = cur?.thoughts || [];
            if (thoughts[thoughts.length - 1] === t) return prev;
            return {
              ...prev,
              [taskId]: {
                toolCalls: cur?.toolCalls || 0,
                lastTool: cur?.lastTool || null,
                summary: cur?.summary || null,
                thoughts: [...thoughts, t].slice(-15),
              },
            };
          });
        }
      } else if (ev.type === "final" || ev.type === "message") {
        try {
          const p = JSON.parse(ev.payload_json);
          if (p.text && ev.type === "final") {
            // Capture final summary
            setTaskProgress((prev) => ({
              ...prev,
              [taskId]: {
                ...prev[taskId],
                toolCalls: prev[taskId]?.toolCalls || 0,
                lastTool: prev[taskId]?.lastTool || null,
                summary: p.text,
              },
            }));
          }
        } catch {
          // ignore parse errors
        }
      }
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
          // Don't auto-open the right panel - key progress now shows inline
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

  useEffect(() => {
    // Auto-scroll when tasks update or live events change
    // Use instant scroll on initial load to avoid the scroll animation
    if (isInitialLoad.current && tasks.length > 0) {
      bottomRef.current?.scrollIntoView({ behavior: "instant" });
      isInitialLoad.current = false;
    } else if (!isInitialLoad.current) {
      bottomRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [tasks.length, liveEvents.length]);

  useEffect(() => {
    attachmentsRef.current = attachments;
  }, [attachments]);

  useEffect(() => {
    return () => {
      for (const item of attachmentsRef.current) {
        if (item.previewUrl) URL.revokeObjectURL(item.previewUrl);
      }
    };
  }, []);

  function addAttachments(files: FileList | File[]) {
    const nextFiles = Array.from(files);
    if (nextFiles.length === 0) return;
    setAttachments((prev) => [
      ...prev,
      ...nextFiles.map((file) => ({
        id: `${file.name}-${file.size}-${file.lastModified}-${crypto.randomUUID()}`,
        file,
        previewUrl: file.type.startsWith("image/")
          ? URL.createObjectURL(file)
          : null,
      })),
    ]);
  }

  function handleDragEnter(e: React.DragEvent) {
    e.preventDefault();
    e.stopPropagation();
    dragCounterRef.current++;
    if (e.dataTransfer.types.includes("Files")) {
      setIsDragging(true);
    }
  }

  function handleDragLeave(e: React.DragEvent) {
    e.preventDefault();
    e.stopPropagation();
    dragCounterRef.current--;
    if (dragCounterRef.current === 0) {
      setIsDragging(false);
    }
  }

  function handleDragOver(e: React.DragEvent) {
    e.preventDefault();
    e.stopPropagation();
  }

  function handleDrop(e: React.DragEvent) {
    e.preventDefault();
    e.stopPropagation();
    dragCounterRef.current = 0;
    setIsDragging(false);
    if (e.dataTransfer.files.length > 0) {
      addAttachments(e.dataTransfer.files);
    }
  }

  function removeAttachment(id: string) {
    setAttachments((prev) => {
      const item = prev.find((a) => a.id === id);
      if (item?.previewUrl) URL.revokeObjectURL(item.previewUrl);
      return prev.filter((a) => a.id !== id);
    });
  }

  function clearAttachments() {
    for (const item of attachments) {
      if (item.previewUrl) URL.revokeObjectURL(item.previewUrl);
    }
    setAttachments([]);
    if (fileInputRef.current) fileInputRef.current.value = "";
  }

  function formatFileSize(size: number) {
    if (size < 1024) return `${size} B`;
    if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
    return `${(size / (1024 * 1024)).toFixed(1)} MB`;
  }

  async function onSend() {
    const trimmedMessage = message.trim();
    const title =
      trimmedMessage ||
      (attachments.length === 1
        ? `Attached file: ${attachments[0].file.name}`
        : `Attached ${attachments.length} files`);
    if ((!trimmedMessage && attachments.length === 0) || sending) return;
    setError(null);
    setSending(true);
    try {
      const task = await api.createTask(projectId, title, "", agent);
      if (attachments.length > 0) {
        await api.uploadTaskAttachments(task.id, attachments.map((a) => a.file));
      }
      setMessage("");
      clearAttachments();
      await load();
      await api.runTask(task.id);
      openStream(task.id);
      // Don't auto-open the right panel - key progress now shows inline
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
      // Don't auto-open the right panel - key progress now shows inline
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
        if (res.dirty) {
          const files = res.dirty_files.length
            ? ` (${res.dirty_files.join(", ")})`
            : "";
          setError(
            `main has uncommitted changes — commit or stash them, then approve again${files}`,
          );
        } else if (res.conflict) {
          setError(`merge conflict: ${res.conflicted_files.join(", ")}`);
        } else if (res.verify_failed) {
          // Gate blocked it: the merge was aborted, main is untouched, and the
          // task stays at awaiting_approval so it can be revised and retried.
          setVerifyFail({ taskId: id, output: res.verify_output });
          setError("verify failed — merge aborted, nothing landed on main.");
        } else {
          setError(res.verify_output || "merge failed");
        }
      } else {
        // Close the panel if merge was successful
        setPanel(null);
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

  function parseQuota(value: string, kind: "tokens" | "cost") {
    const trimmed = value.trim();
    if (!trimmed) return null;
    const parsed =
      kind === "tokens"
        ? Number.parseInt(trimmed, 10)
        : Number.parseFloat(trimmed);
    if (!Number.isFinite(parsed) || parsed < 0) {
      throw new Error(
        kind === "tokens"
          ? "Token budget must be a non-negative number"
          : "Cost budget must be a non-negative number",
      );
    }
    return parsed;
  }

  async function saveBudget() {
    setSavingBudget(true);
    setError(null);
    try {
      const quotaTokens = parseQuota(quotaTokensDraft, "tokens");
      const quotaCost = parseQuota(quotaCostDraft, "cost");
      const updated = await api.updateProjectQuotas(
        projectId,
        quotaTokens,
        quotaCost,
      );
      const updatedUsage = await api.getProjectUsage(projectId);
      setProject(updated);
      setUsage(updatedUsage);
      setEditingBudget(false);
    } catch (e) {
      setError(String(e));
    } finally {
      setSavingBudget(false);
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

  async function onStop(id: number) {
    setError(null);
    try {
      await api.stopTask(id);
      // Close the event source if it's for this task
      if (esRef.current && streamingTaskId === id) {
        esRef.current.close();
        esRef.current = null;
      }
      setStreamingTaskId(null);
      setLiveEvents([]);
      await load();
    } catch (e) {
      setError(String(e));
    }
  }

  function startFollowUp(sourceTaskId: number) {
    setFollowUpSourceId(sourceTaskId);
    setMessage(""); // Clear any existing message for fresh follow-up input
  }

  function cancelFollowUp() {
    setFollowUpSourceId(null);
    setMessage("");
  }

  async function createFollowUp() {
    const trimmedMessage = message.trim();
    if (!followUpSourceId || !trimmedMessage) return;
    setCreatingFollowUp(true);
    setError(null);
    try {
      const task = await api.createTask(
        projectId,
        trimmedMessage,
        "",
        agent,
        followUpSourceId,
      );
      setFollowUpSourceId(null);
      setMessage("");
      clearAttachments();
      await load();
      // Auto-run the new task
      await api.runTask(task.id);
      openStream(task.id);
    } catch (e) {
      setError(String(e));
    } finally {
      setCreatingFollowUp(false);
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
  // Show every option including "auto" (smart routing). The /agents list puts
  // "auto" first, so it leads the dropdown and is the default.
  const realAgents = agents;
  const enabledSkills: string[] = (() => {
    try {
      return project?.enabled_skills ? JSON.parse(project.enabled_skills) : [];
    } catch {
      return [];
    }
  })();

  // Collect all unique tags from tasks
  const allTags = Array.from(
    new Set(
      tasks.flatMap((t) => {
        if (!t.tags) return [];
        try {
          return JSON.parse(t.tags) as string[];
        } catch {
          return [];
        }
      }),
    ),
  ).sort();

  // Filter tasks by selected tags
  const filteredTasks =
    selectedTags.size === 0
      ? tasks
      : tasks.filter((t) => {
          if (!t.tags) return false;
          try {
            const taskTags = new Set(JSON.parse(t.tags) as string[]);
            return Array.from(selectedTags).some((tag) => taskTags.has(tag));
          } catch {
            return false;
          }
        });

  function toggleTag(tag: string) {
    setSelectedTags((prev) => {
      const next = new Set(prev);
      if (next.has(tag)) {
        next.delete(tag);
      } else {
        next.add(tag);
      }
      return next;
    });
  }

  const tokenPercent = usage?.usage_percentage.tokens ?? null;
  const costPercent = usage?.usage_percentage.cost ?? null;
  const budgetBarPercent = Math.min(
    100,
    Math.max(0, tokenPercent ?? 0, costPercent ?? 0),
  );
  const budgetWarning =
    (tokenPercent != null && tokenPercent >= 90) ||
    (costPercent != null && costPercent >= 90);
  const budgetOver =
    (tokenPercent != null && tokenPercent >= 100) ||
    (costPercent != null && costPercent >= 100);
  const formatTokens = (value: number) => value.toLocaleString();
  const formatCost = (value: number) => `$${value.toFixed(4)}`;

  return (
    <div className="flex h-[calc(100vh-9rem)] gap-4">
      <ProjectSidebar
        projects={projects}
        currentId={projectId}
        onChanged={load}
      />

      <div className="relative flex flex-1 overflow-hidden">
        <div className="flex flex-1 flex-col overflow-hidden">
          <div className="mb-2 flex items-center gap-3">
            <h1 className="truncate text-lg font-semibold">
              {project?.name ?? `Project ${projectId}`}
            </h1>
            <div className="ml-auto flex items-center gap-4">
              <AgentsHealth />
              <SkillsControl
                projectId={projectId}
                enabled={enabledSkills}
                onChanged={load}
              />
              <DistillButton projectId={projectId} />
              <AutoHealControl
                projectId={projectId}
                rounds={project?.auto_heal_rounds ?? 0}
                onChanged={load}
              />
            </div>
            <div className="flex items-center gap-2 text-xs">
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
                    className="w-80 rounded-md border border-border bg-card px-2 py-1 font-mono outline-none focus:border-ring"
                    autoFocus
                  />
                  <Button onClick={saveVerify} disabled={savingVerify} className="px-2 py-1">
                    {savingVerify ? "Saving…" : "Save"}
                  </Button>
                  <button
                    type="button"
                    onClick={() => setEditingVerify(false)}
                    className="text-muted-foreground hover:text-foreground"
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
                  className="flex max-w-md items-center gap-1.5 rounded-md border border-border px-2 py-1 hover:border-border"
                >
                  <span className="text-muted-foreground">verify gate:</span>
                  <span className="truncate font-mono text-foreground">
                    {project?.verify_cmd ? project.verify_cmd : "off"}
                  </span>
                </button>
              )}
            </div>
          </div>

          <div className="mb-2 rounded-md border border-border bg-card/40 px-3 py-2 text-xs">
            <div className="flex flex-wrap items-center gap-3">
              <span className="font-medium text-foreground">Budget</span>
              {usage && (
                <>
                  <span className="text-muted-foreground">
                    {formatTokens(usage.usage.total_tokens)} tokens
                    {usage.quotas.quota_tokens != null
                      ? ` / ${formatTokens(usage.quotas.quota_tokens)}`
                      : " / unlimited"}
                  </span>
                  <span className="text-muted-foreground">
                    {formatCost(usage.usage.total_cost_usd)}
                    {usage.quotas.quota_cost_usd != null
                      ? ` / $${usage.quotas.quota_cost_usd.toFixed(2)}`
                      : " / unlimited"}
                  </span>
                  <span className="text-muted-foreground">
                    {usage.usage.run_count} completed runs
                  </span>
                </>
              )}
              <button
                type="button"
                onClick={() => {
                  setQuotaTokensDraft(project?.quota_tokens?.toString() ?? "");
                  setQuotaCostDraft(project?.quota_cost_usd?.toString() ?? "");
                  setEditingBudget(true);
                }}
                className="ml-auto text-muted-foreground hover:text-foreground"
              >
                Edit
              </button>
            </div>
            {(usage?.quotas.quota_tokens != null ||
              usage?.quotas.quota_cost_usd != null) && (
              <div className="mt-2 h-1.5 overflow-hidden rounded bg-muted">
                <div
                  className={`h-full rounded ${
                    budgetOver
                      ? "bg-red-500"
                      : budgetWarning
                        ? "bg-amber-500"
                        : "bg-green-500"
                  }`}
                  style={{ width: `${budgetBarPercent}%` }}
                />
              </div>
            )}
            {editingBudget && (
              <div className="mt-2 flex flex-wrap items-center gap-2">
                <input
                  value={quotaTokensDraft}
                  onChange={(e) => setQuotaTokensDraft(e.target.value)}
                  placeholder="Token budget"
                  inputMode="numeric"
                  className="w-36 rounded-md border border-border bg-background px-2 py-1 outline-none focus:border-ring"
                />
                <input
                  value={quotaCostDraft}
                  onChange={(e) => setQuotaCostDraft(e.target.value)}
                  placeholder="Cost budget USD"
                  inputMode="decimal"
                  className="w-36 rounded-md border border-border bg-background px-2 py-1 outline-none focus:border-ring"
                />
                <Button
                  onClick={saveBudget}
                  disabled={savingBudget}
                  className="px-2 py-1"
                >
                  {savingBudget ? "Saving…" : "Save"}
                </Button>
                <button
                  type="button"
                  onClick={() => setEditingBudget(false)}
                  className="text-muted-foreground hover:text-foreground"
                >
                  Cancel
                </button>
                <span className="text-muted-foreground">
                  Leave blank for unlimited.
                </span>
              </div>
            )}
          </div>

          {/* Tag filter bar */}
          {allTags.length > 0 && (
            <div className="mb-2 flex flex-wrap items-center gap-2">
              <span className="text-xs text-muted-foreground">Filter by tag:</span>
              {allTags.map((tag) => (
                <button
                  key={tag}
                  type="button"
                  onClick={() => toggleTag(tag)}
                  className={`rounded-md border px-2 py-0.5 text-xs transition-colors ${
                    selectedTags.has(tag)
                      ? "border-blue-600 bg-blue-950/60 text-blue-300"
                      : "border-border bg-card/40 text-muted-foreground hover:border-ring hover:text-foreground"
                  }`}
                >
                  {tag}
                </button>
              ))}
              {selectedTags.size > 0 && (
                <button
                  type="button"
                  onClick={() => setSelectedTags(new Set())}
                  className="text-xs text-muted-foreground hover:text-foreground"
                >
                  Clear
                </button>
              )}
            </div>
          )}

          <div className="flex-1 space-y-4 overflow-y-auto rounded-lg border border-border bg-background/40 p-4">
            {tasks.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                No tasks yet. Send a message below to create one.
              </p>
            ) : filteredTasks.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                No tasks match the selected tags.
              </p>
            ) : (
              filteredTasks.map((t) => (
                <ConversationTurn
                  key={`${t.id}:${reloadNonce}`}
                  task={t}
                  hasChildren={childParentIds.has(t.id)}
                  streaming={streamingTaskId === t.id}
                  liveEvents={streamingTaskId === t.id ? liveEvents : []}
                  busy={busyTaskId === t.id}
                  activeKind={panel?.taskId === t.id ? panel.kind : null}
                  persistedProgress={taskProgress[t.id]}
                  onRun={onRun}
                  onReview={onReview}
                  onApprove={onApprove}
                  onReject={onReject}
                  onOpenPanel={openPanel}
                  onStop={onStop}
                  onCreateFollowUp={startFollowUp}
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
              <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words font-mono text-xs text-foreground">
                {verifyFail.output || "(no output)"}
              </pre>
            </div>
          )}

          <div
            className={`relative mt-2 flex items-end gap-2 rounded-lg border-2 p-2 transition-colors ${
              isDragging
                ? "border-dashed border-blue-500 bg-blue-500/10"
                : followUpSourceId !== null
                  ? "border-blue-600/50 bg-blue-950/20"
                  : "border-transparent"
            }`}
            onDragEnter={handleDragEnter}
            onDragLeave={handleDragLeave}
            onDragOver={handleDragOver}
            onDrop={handleDrop}
          >
            {isDragging && (
              <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center rounded-lg bg-blue-500/10">
                <span className="text-lg font-medium text-blue-400">
                  Drop files here to attach
                </span>
              </div>
            )}
            <div className="flex flex-1 flex-col gap-2">
              {/* Follow-up mode indicator */}
              {followUpSourceId !== null && (
                <div className="flex items-center justify-between rounded-md bg-blue-950/40 px-3 py-1.5 text-sm">
                  <span className="text-blue-300">
                    Follow-up to task #{followUpSourceId} — agent will inherit previous context
                  </span>
                  <button
                    type="button"
                    onClick={cancelFollowUp}
                    className="ml-2 text-blue-400 hover:text-blue-200"
                  >
                    Cancel
                  </button>
                </div>
              )}
              {attachments.length > 0 && (
                <div className="flex flex-wrap gap-2">
                  {attachments.map((item) => (
                    <div
                      key={item.id}
                      className="flex max-w-56 items-center gap-2 rounded-md border border-border bg-card px-2 py-1"
                    >
                      {item.previewUrl ? (
                        // eslint-disable-next-line @next/next/no-img-element
                        <img
                          src={item.previewUrl}
                          alt=""
                          className="h-9 w-9 rounded object-cover"
                        />
                      ) : (
                        <span className="flex h-9 w-9 items-center justify-center rounded bg-muted text-xs">
                          file
                        </span>
                      )}
                      <div className="min-w-0 flex-1">
                        <div className="truncate text-xs text-foreground">
                          {item.file.name}
                        </div>
                        <div className="text-[11px] text-muted-foreground">
                          {formatFileSize(item.file.size)}
                        </div>
                      </div>
                      <button
                        type="button"
                        onClick={() => removeAttachment(item.id)}
                        aria-label={`Remove ${item.file.name}`}
                        className="text-muted-foreground hover:text-foreground"
                      >
                        x
                      </button>
                    </div>
                  ))}
                </div>
              )}
              <textarea
                value={message}
                onChange={(e) => setMessage(e.target.value)}
                onPaste={(e) => {
                  if (e.clipboardData.files.length > 0) {
                    addAttachments(e.clipboardData.files);
                  }
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    if (followUpSourceId !== null) {
                      createFollowUp();
                    } else {
                      onSend();
                    }
                  }
                  if (e.key === "Escape" && followUpSourceId !== null) {
                    cancelFollowUp();
                  }
                }}
                rows={2}
                placeholder={
                  followUpSourceId !== null
                    ? "What should the agent do next? (Enter to send, Esc to cancel)"
                    : "Describe a task… (drop files here, Enter to send)"
                }
                className="resize-none rounded-md border border-border bg-card px-3 py-2 text-sm outline-none focus:border-ring"
                autoFocus={followUpSourceId !== null}
              />
            </div>
            <input
              ref={fileInputRef}
              type="file"
              multiple
              className="hidden"
              onChange={(e) => {
                if (e.target.files) addAttachments(e.target.files);
              }}
            />
            <Button
              variant="ghost"
              type="button"
              onClick={() => fileInputRef.current?.click()}
              className="px-3"
              title="Attach files or screenshots (or drag & drop)"
            >
              Attach
            </Button>
            <select
              value={agent}
              onChange={(e) => setAgent(e.target.value)}
              className="rounded-md border border-border bg-card px-2 py-2 text-sm outline-none focus:border-ring"
            >
              {realAgents.map((a) => (
                <option key={a.name} value={a.name}>
                  {a.name}
                </option>
              ))}
            </select>
            <Button
              onClick={followUpSourceId !== null ? createFollowUp : onSend}
              disabled={
                (followUpSourceId !== null ? creatingFollowUp : sending) ||
                (!message.trim() && attachments.length === 0)
              }
              className="px-4"
            >
              {followUpSourceId !== null
                ? creatingFollowUp
                  ? "Creating…"
                  : "Follow-up"
                : sending
                  ? "Sending…"
                  : "Send"}
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

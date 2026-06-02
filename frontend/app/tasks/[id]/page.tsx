"use client";

import { useEffect, useRef, useState } from "react";
import {
  api,
  type Event as ApiEvent,
  type Review,
  type Run,
  type Task,
} from "@/lib/api";
import { Badge, Button, Card } from "@/components/ui";
import { NextStepsModal } from "@/components/NextStepsModal";

interface RunWithEvents extends Run {
  events: ApiEvent[];
}

function parsePayload(json: string): { text?: string; data?: Record<string, unknown> } {
  try {
    return JSON.parse(json);
  } catch {
    return {};
  }
}

const EVENT_COLOR: Record<string, string> = {
  meta: "text-muted-foreground",
  message: "text-foreground",
  thinking: "text-indigo-300",
  tool_use: "text-blue-300",
  tool_result: "text-muted-foreground",
  final: "text-green-300",
  cost: "text-muted-foreground",
  error: "text-red-400",
  diff_ready: "text-muted-foreground",
};

function EventRow({ ev }: { ev: ApiEvent }) {
  const { text, data } = parsePayload(ev.payload_json);
  const color = EVENT_COLOR[ev.type] ?? "text-foreground";
  const detail = text ?? (data ? JSON.stringify(data) : "");
  return (
    <div className="flex gap-2">
      <span className="shrink-0 text-muted-foreground">{ev.type}</span>
      <span className={`whitespace-pre-wrap break-words ${color}`}>{detail}</span>
    </div>
  );
}

export default function TaskPage({ params }: { params: { id: string } }) {
  const taskId = Number(params.id);
  const [task, setTask] = useState<Task | null>(null);
  const [children, setChildren] = useState<Task[]>([]);
  const [runs, setRuns] = useState<RunWithEvents[]>([]);
  const [liveEvents, setLiveEvents] = useState<ApiEvent[]>([]);
  const [diff, setDiff] = useState<string>("");
  const [review, setReview] = useState<Review | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [planning, setPlanning] = useState(false);
  const [reviewing, setReviewing] = useState(false);
  const [acting, setActing] = useState(false);
  const [showNextStepsModal, setShowNextStepsModal] = useState(false);
  const esRef = useRef<EventSource | null>(null);

  async function load(): Promise<Task | null> {
    try {
      const t = await api.getTask(taskId);
      setTask(t);
      const siblings = await api.listTasks(t.project_id);
      setChildren(siblings.filter((s) => s.parent_id === taskId));
      const baseRuns = await api.listRuns(taskId);
      const withEvents = await Promise.all(
        baseRuns.map(async (r) => ({
          ...r,
          events: await api.listEvents(r.id),
        })),
      );
      setRuns(withEvents);
      if (["awaiting_approval", "merged", "failed"].includes(t.status)) {
        try {
          setDiff((await api.getDiff(taskId)).diff);
        } catch {
          setDiff("");
        }
      } else {
        setDiff("");
      }
      if (
        ["awaiting_approval", "merged", "rejected", "failed"].includes(t.status)
      ) {
        try {
          setReview(await api.getReview(taskId));
        } catch {
          setReview(null);
        }
      } else {
        setReview(null);
      }
      return t;
    } catch (e) {
      setError(String(e));
      return null;
    }
  }

  // Open the SSE stream for the latest run; the backend replays from seq 0, so
  // a fresh run and a resume-on-mount are the same code path.
  function openStream() {
    esRef.current?.close();
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
      const updatedTask = await load();
      setLiveEvents([]);
      setRunning(false);
      // Show next steps modal if task is now awaiting approval
      if (updatedTask?.status === "awaiting_approval") {
        setShowNextStepsModal(true);
      }
    });

    es.onerror = async () => {
      if (finished) return; // normal server close; already handled by "done"
      es.close();
      if (esRef.current === es) esRef.current = null;
      setError("stream disconnected");
      await load();
      setLiveEvents([]);
      setRunning(false);
    };
  }

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const t = await load();
      if (cancelled || !t) return;
      // A run already in flight (navigated away and back, or reload): resume the
      // live stream rather than leaving the UI frozen.
      if (t.status === "running") {
        setRunning(true);
        openStream();
      }
    })();
    return () => {
      cancelled = true;
      esRef.current?.close();
      esRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskId]);

  async function onRun() {
    setError(null);
    setLiveEvents([]);
    setReview(null);
    setRunning(true);
    try {
      await api.runTask(taskId); // 202 — returns immediately
    } catch (e) {
      setError(String(e));
      setRunning(false);
      return;
    }
    try {
      setTask(await api.getTask(taskId));
    } catch {
      // non-fatal: the stream still drives the UI
    }
    openStream();
  }

  async function onPlan() {
    setError(null);
    setPlanning(true);
    try {
      await api.planTask(taskId);
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setPlanning(false);
    }
  }

  async function onReview() {
    setError(null);
    setReviewing(true);
    try {
      setReview(await api.reviewTask(taskId));
    } catch (e) {
      setError(String(e));
    } finally {
      setReviewing(false);
    }
  }

  // Address the latest review's findings by re-running in the same worktree.
  // Mirrors onRun: the backend flips to running and replays via the same stream.
  async function onRevise() {
    setError(null);
    setLiveEvents([]);
    setReview(null);
    setRunning(true);
    try {
      await api.reviseTask(taskId); // 202 — returns immediately
    } catch (e) {
      setError(String(e));
      setRunning(false);
      return;
    }
    try {
      setTask(await api.getTask(taskId));
    } catch {
      // non-fatal: the stream still drives the UI
    }
    openStream();
  }

  async function onApprove() {
    setError(null);
    setActing(true);
    try {
      const res = await api.approve(taskId);
      if (!res.ok && res.conflict) {
        setError(`merge conflict: ${res.conflicted_files.join(", ")}`);
      }
      await load();
      // Show next steps modal after successful approval
      if (res.ok) {
        setShowNextStepsModal(true);
      }
    } catch (e) {
      setError(String(e));
    } finally {
      setActing(false);
    }
  }

  async function onReject() {
    setError(null);
    setActing(true);
    try {
      await api.reject(taskId);
      await load();
      // Show next steps modal after rejection
      setShowNextStepsModal(true);
    } catch (e) {
      setError(String(e));
    } finally {
      setActing(false);
    }
  }

  async function handleTasksSelected(taskIds: number[]) {
    // Start the selected tasks
    try {
      for (const tid of taskIds) {
        await api.runTask(tid);
      }
      // Navigate to the first task if only one selected
      if (taskIds.length === 1) {
        window.location.href = `/tasks/${taskIds[0]}`;
      }
    } catch (e) {
      setError(String(e));
    }
  }

  const isContainer = children.length > 0;
  const canRun =
    (task?.status === "draft" || task?.status === "failed") && !isContainer;
  const canPlan = task?.status === "draft" && !isContainer;
  const canApprove = task?.status === "awaiting_approval";
  // The in-flight run is shown by the live card; hide its duplicate in history.
  const visibleRuns = runs.filter((r) => !(running && r.status === "running"));

  return (
    <div className="space-y-8">
      <div>
        {task && (
          <a
            href={
              task.parent_id
                ? `/tasks/${task.parent_id}`
                : `/projects/${task.project_id}`
            }
            className="text-xs text-muted-foreground hover:text-foreground"
          >
            {task.parent_id ? "← parent task" : "← project"}
          </a>
        )}
        <div className="mt-2 flex items-center gap-3">
          <h1 className="text-xl font-semibold">
            {task?.title ?? `Task ${taskId}`}
          </h1>
          {task && <Badge status={task.status} />}
        </div>
        {task?.description && (
          <p className="mt-2 whitespace-pre-wrap text-sm text-muted-foreground">
            {task.description}
          </p>
        )}
        {task && (
          <div className="mt-1 text-xs text-muted-foreground">
            agent: {task.assigned_agent ?? "—"}
            {task.branch ? ` · ${task.branch}` : ""}
          </div>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <Button onClick={onRun} disabled={!canRun || running}>
          {running ? "Running…" : "Run"}
        </Button>
        {canPlan && (
          <Button variant="ghost" onClick={onPlan} disabled={planning}>
            {planning ? "Planning…" : "Plan → subtasks"}
          </Button>
        )}
        {canApprove && (
          <Button variant="ghost" onClick={onReview} disabled={reviewing || acting}>
            {reviewing ? "Reviewing…" : review ? "Re-review" : "AI Review"}
          </Button>
        )}
        {canApprove && review?.verdict === "request_changes" && (
          <Button
            variant="ghost"
            onClick={onRevise}
            disabled={running || reviewing || acting}
          >
            Revise
          </Button>
        )}
        <Button variant="ghost" onClick={onApprove} disabled={!canApprove || acting}>
          Approve &amp; merge
        </Button>
        <Button variant="danger" onClick={onReject} disabled={!canApprove || acting}>
          Reject
        </Button>
      </div>

      {children.length > 0 && (
        <section>
          <h2 className="mb-3 text-sm font-semibold text-muted-foreground">Subtasks</h2>
          <div className="space-y-2">
            {children.map((c) => (
              <a key={c.id} href={`/tasks/${c.id}`} className="block">
                <Card className="hover:border-ring">
                  <div className="flex items-center justify-between gap-3">
                    <span className="truncate font-medium">{c.title}</span>
                    <Badge status={c.status} />
                  </div>
                  <div className="mt-1 text-xs text-muted-foreground">
                    agent: {c.assigned_agent ?? "—"}
                    {c.branch ? ` · ${c.branch}` : ""}
                  </div>
                </Card>
              </a>
            ))}
          </div>
        </section>
      )}

      {running && (
        <section>
          <h2 className="mb-3 text-sm font-semibold text-muted-foreground">Live run</h2>
          <Card>
            <div className="mb-3 flex items-center justify-between text-xs text-muted-foreground">
              <span>{task?.assigned_agent ?? "agent"} · streaming</span>
              <span className="flex items-center gap-2">
                <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-green-400" />
                live
              </span>
            </div>
            <div className="space-y-1 font-mono text-xs">
              {liveEvents.length === 0 ? (
                <div className="text-muted-foreground">waiting for first event…</div>
              ) : (
                liveEvents.map((ev) => <EventRow key={ev.id} ev={ev} />)
              )}
            </div>
          </Card>
        </section>
      )}

      <section>
        <h2 className="mb-3 text-sm font-semibold text-muted-foreground">Runs</h2>
        {visibleRuns.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            {running ? "Run in progress…" : "No runs yet."}
          </p>
        ) : (
          <div className="space-y-4">
            {visibleRuns.map((r) => (
              <Card key={r.id}>
                <div className="mb-3 flex items-center justify-between text-xs text-muted-foreground">
                  <span>
                    run #{r.id} · {r.agent}
                    {r.session_id ? ` · ${r.session_id}` : ""}
                  </span>
                  <span className="flex items-center gap-2">
                    <Badge status={r.status} />
                    <span>
                      {r.tokens_in}/{r.tokens_out} tok · ${r.cost.toFixed(4)}
                    </span>
                  </span>
                </div>
                <div className="space-y-1 font-mono text-xs">
                  {r.events.map((ev) => (
                    <EventRow key={ev.id} ev={ev} />
                  ))}
                </div>
              </Card>
            ))}
          </div>
        )}
      </section>

      {review && (
        <section>
          <h2 className="mb-3 text-sm font-semibold text-muted-foreground">AI Review</h2>
          <Card>
            <div className="mb-3 flex items-center justify-between text-xs text-muted-foreground">
              <span>{review.agent}</span>
              <Badge status={review.verdict} />
            </div>
            {review.summary && (
              <p className="mb-3 whitespace-pre-wrap text-sm text-foreground">
                {review.summary}
              </p>
            )}
            {review.findings.length === 0 ? (
              <p className="text-xs text-muted-foreground">No findings.</p>
            ) : (
              <ul className="space-y-2">
                {review.findings.map((f, i) => (
                  <li key={i} className="flex gap-2 text-sm">
                    <Badge status={f.severity} />
                    <span className="text-foreground">
                      {f.file && (
                        <span className="font-mono text-xs text-muted-foreground">
                          {f.file}:{" "}
                        </span>
                      )}
                      {f.comment}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </section>
      )}

      {diff && (
        <section>
          <h2 className="mb-3 text-sm font-semibold text-muted-foreground">Diff</h2>
          <pre className="overflow-x-auto rounded-lg border border-border bg-card/50 p-4 text-xs leading-relaxed text-foreground">
            {diff}
          </pre>
        </section>
      )}

      {error && <p className="text-sm text-red-400">{error}</p>}

      {showNextStepsModal && (
        <NextStepsModal
          taskId={taskId}
          onClose={() => setShowNextStepsModal(false)}
          onTasksSelected={handleTasksSelected}
        />
      )}
    </div>
  );
}

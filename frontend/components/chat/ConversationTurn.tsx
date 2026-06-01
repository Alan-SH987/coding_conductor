"use client";

import Link from "next/link";
import {
  api,
  type Event as ApiEvent,
  type Review,
  type Run,
  type Task,
} from "@/lib/api";
import { Badge, Button } from "@/components/ui";
import { Collapsible, DiffView } from "@/components/chat/Collapsible";

interface RunWithEvents extends Run {
  events: ApiEvent[];
}

const EVENT_COLOR: Record<string, string> = {
  meta: "text-zinc-500",
  message: "text-zinc-100",
  thinking: "text-indigo-300",
  tool_use: "text-blue-300",
  tool_result: "text-zinc-400",
  final: "text-green-300",
  cost: "text-zinc-500",
  error: "text-red-400",
  diff_ready: "text-zinc-500",
};

function eventDetail(json: string): string {
  try {
    const p = JSON.parse(json);
    return p.text ?? (p.data ? JSON.stringify(p.data) : "");
  } catch {
    return "";
  }
}

function EventLog({ events }: { events: ApiEvent[] }) {
  if (events.length === 0) {
    return <div className="text-xs text-zinc-600">No events.</div>;
  }
  return (
    <div className="space-y-1 font-mono text-xs">
      {events.map((ev) => (
        <div key={ev.id} className="flex gap-2">
          <span className="shrink-0 text-zinc-600">{ev.type}</span>
          <span
            className={`whitespace-pre-wrap break-words ${
              EVENT_COLOR[ev.type] ?? "text-zinc-300"
            }`}
          >
            {eventDetail(ev.payload_json)}
          </span>
        </div>
      ))}
    </div>
  );
}

// One task rendered as a conversation turn: a right-aligned "user" bubble (the
// prompt) and a left-aligned "assistant" block (status, inline actions, live
// stream while running, and lazy-loaded Activity / Diff / Review cards).
export function ConversationTurn({
  task,
  hasChildren,
  streaming,
  liveEvents,
  busy,
  onRun,
  onReview,
  onApprove,
  onReject,
}: {
  task: Task;
  hasChildren: boolean;
  streaming: boolean;
  liveEvents: ApiEvent[];
  busy: boolean;
  onRun: (id: number) => void;
  onReview: (id: number) => void;
  onApprove: (id: number) => void;
  onReject: (id: number) => void;
}) {
  const canRun =
    (task.status === "draft" || task.status === "failed") && !hasChildren;
  const atGate = task.status === "awaiting_approval";
  const showActivity =
    !hasChildren && task.status !== "draft" && task.status !== "planned";
  const showDiff = ["awaiting_approval", "merged", "failed"].includes(
    task.status,
  );
  const showReview = [
    "awaiting_approval",
    "merged",
    "rejected",
    "failed",
  ].includes(task.status);

  return (
    <div className="space-y-2">
      <div className="flex justify-end">
        <div className="max-w-[85%] rounded-lg rounded-br-sm bg-zinc-800 px-3 py-2 text-sm text-zinc-100">
          {task.parent_id && (
            <div className="mb-1 text-[10px] text-zinc-500">
              ↳ subtask of #{task.parent_id}
            </div>
          )}
          <div className="whitespace-pre-wrap break-words">{task.title}</div>
          {task.description && (
            <div className="mt-1 whitespace-pre-wrap break-words text-xs text-zinc-400">
              {task.description}
            </div>
          )}
        </div>
      </div>

      <div className="max-w-[92%] space-y-2 rounded-lg rounded-bl-sm border border-zinc-800 bg-zinc-900/40 px-3 py-2">
        <div className="flex flex-wrap items-center gap-2 text-xs text-zinc-500">
          <span className="font-mono">#{task.id}</span>
          <Badge status={task.status} />
          <span>{task.assigned_agent ?? "—"}</span>
          {task.branch && <span className="font-mono">{task.branch}</span>}
          <Link
            href={`/tasks/${task.id}`}
            className="ml-auto text-zinc-500 hover:text-zinc-300"
          >
            open ↗
          </Link>
        </div>

        {(canRun || atGate) && (
          <div className="flex flex-wrap gap-2">
            {canRun && (
              <Button onClick={() => onRun(task.id)} disabled={busy || streaming}>
                {streaming
                  ? "Running…"
                  : task.status === "failed"
                    ? "Retry"
                    : "Run"}
              </Button>
            )}
            {atGate && (
              <>
                <Button
                  variant="ghost"
                  onClick={() => onReview(task.id)}
                  disabled={busy}
                >
                  AI Review
                </Button>
                <Button
                  variant="ghost"
                  onClick={() => onApprove(task.id)}
                  disabled={busy}
                >
                  Approve &amp; merge
                </Button>
                <Button
                  variant="danger"
                  onClick={() => onReject(task.id)}
                  disabled={busy}
                >
                  Reject
                </Button>
              </>
            )}
          </div>
        )}

        {streaming && (
          <div className="rounded-md border border-zinc-800 bg-zinc-950/60 px-3 py-2">
            <div className="mb-1 flex items-center gap-2 text-[10px] text-zinc-500">
              <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-green-400" />
              live
            </div>
            {liveEvents.length === 0 ? (
              <div className="text-xs text-zinc-600">
                waiting for first event…
              </div>
            ) : (
              <EventLog events={liveEvents} />
            )}
          </div>
        )}

        {showActivity && (
          <Collapsible
            title="Activity"
            loader={async () => {
              const runs = await api.listRuns(task.id);
              return Promise.all(
                runs.map(async (r) => ({
                  ...r,
                  events: await api.listEvents(r.id),
                })),
              );
            }}
          >
            {(runs: RunWithEvents[]) =>
              runs.length === 0 ? (
                <div className="text-xs text-zinc-600">No runs yet.</div>
              ) : (
                <div className="space-y-3">
                  {runs.map((r) => (
                    <div key={r.id}>
                      <div className="mb-1 flex items-center justify-between text-[10px] text-zinc-500">
                        <span>
                          run #{r.id} · {r.agent}
                        </span>
                        <span>
                          {r.tokens_in}/{r.tokens_out} tok · $
                          {r.cost.toFixed(4)}
                        </span>
                      </div>
                      <EventLog events={r.events} />
                    </div>
                  ))}
                </div>
              )
            }
          </Collapsible>
        )}

        {showDiff && (
          <Collapsible
            title="Diff"
            loader={async () => (await api.getDiff(task.id)).diff}
          >
            {(diff: string) => <DiffView diff={diff} />}
          </Collapsible>
        )}

        {showReview && (
          <Collapsible title="AI Review" loader={() => api.getReview(task.id)}>
            {(review: Review | null) =>
              review === null ? (
                <div className="text-xs text-zinc-600">No review yet.</div>
              ) : (
                <div className="space-y-2">
                  <div className="flex items-center gap-2 text-xs text-zinc-500">
                    <span>{review.agent}</span>
                    <Badge status={review.verdict} />
                  </div>
                  {review.summary && (
                    <p className="whitespace-pre-wrap text-sm text-zinc-300">
                      {review.summary}
                    </p>
                  )}
                  {review.findings.length === 0 ? (
                    <div className="text-xs text-zinc-600">No findings.</div>
                  ) : (
                    <ul className="space-y-1">
                      {review.findings.map((f, i) => (
                        <li key={i} className="flex gap-2 text-sm">
                          <Badge status={f.severity} />
                          <span className="text-zinc-300">
                            {f.file && (
                              <span className="font-mono text-xs text-zinc-500">
                                {f.file}:{" "}
                              </span>
                            )}
                            {f.comment}
                          </span>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              )
            }
          </Collapsible>
        )}
      </div>
    </div>
  );
}

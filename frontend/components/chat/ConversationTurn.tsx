"use client";

import Link from "next/link";
import { type Task, type Event as ApiEvent } from "@/lib/api";
import { Badge, Button } from "@/components/ui";
import type { PanelKind } from "@/components/chat/RightPanel";

// Extract key progress info from live events for inline display
interface ProgressInfo {
  currentAction: string | null; // What the agent is doing now
  toolCalls: number; // Number of tool calls made
  lastTool: string | null; // Last tool used
  hasThinking: boolean; // Agent is thinking
}

function extractProgress(events: ApiEvent[]): ProgressInfo {
  let currentAction: string | null = null;
  let toolCalls = 0;
  let lastTool: string | null = null;
  let hasThinking = false;

  for (const ev of events) {
    if (ev.type === "thinking") {
      hasThinking = true;
      try {
        const p = JSON.parse(ev.payload_json);
        if (p.text) {
          // Truncate thinking to first line or 60 chars
          const text = p.text.split("\n")[0];
          currentAction = text.length > 60 ? text.slice(0, 57) + "..." : text;
        }
      } catch {
        // ignore
      }
    } else if (ev.type === "tool_use") {
      toolCalls++;
      try {
        const p = JSON.parse(ev.payload_json);
        lastTool = p.tool || p.name || null;
        if (lastTool) {
          currentAction = `Using ${lastTool}`;
        }
      } catch {
        // ignore
      }
    } else if (ev.type === "message") {
      try {
        const p = JSON.parse(ev.payload_json);
        if (p.text) {
          const text = p.text.split("\n")[0];
          currentAction = text.length > 80 ? text.slice(0, 77) + "..." : text;
        }
      } catch {
        // ignore
      }
    }
  }

  return { currentAction, toolCalls, lastTool, hasThinking };
}

function PanelChip({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`rounded-md border px-2 py-1 text-xs transition-colors ${
        active
          ? "border-zinc-600 bg-zinc-800 text-zinc-100"
          : "border-zinc-800 bg-zinc-900/40 text-zinc-400 hover:border-zinc-700 hover:text-zinc-200"
      }`}
    >
      {label}
    </button>
  );
}

// One task rendered as a conversation turn: a right-aligned "user" bubble (the
// prompt) and a left-aligned "assistant" block (status + inline actions). When
// streaming, key progress info shows inline so users can see what's happening
// without opening the right panel.
// Persisted progress info for completed/running tasks
interface TaskProgressInfo {
  toolCalls: number;
  lastTool: string | null;
  summary: string | null;
}

export function ConversationTurn({
  task,
  hasChildren,
  streaming,
  liveEvents,
  busy,
  activeKind,
  persistedProgress,
  onRun,
  onReview,
  onApprove,
  onReject,
  onOpenPanel,
  onStop,
}: {
  task: Task;
  hasChildren: boolean;
  streaming: boolean;
  liveEvents: ApiEvent[];
  busy: boolean;
  activeKind: PanelKind | null;
  persistedProgress?: TaskProgressInfo;
  onRun: (id: number) => void;
  onReview: (id: number) => void;
  onApprove: (id: number) => void;
  onReject: (id: number) => void;
  onOpenPanel: (kind: PanelKind, taskId: number) => void;
  onStop?: (id: number) => void;
}) {
  const progress = streaming ? extractProgress(liveEvents) : null;
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
          {/* Display task tags */}
          {task.tags && (() => {
            try {
              const tags: string[] = JSON.parse(task.tags);
              return tags.map((tag) => (
                <span
                  key={tag}
                  className="rounded-sm bg-zinc-800 px-1.5 py-0.5 text-[10px] text-zinc-400"
                >
                  {tag}
                </span>
              ));
            } catch {
              return null;
            }
          })()}
          <Link
            href={`/tasks/${task.id}`}
            className="ml-auto text-zinc-500 hover:text-zinc-300"
          >
            open ↗
          </Link>
        </div>

        {/* Inline progress display when streaming */}
        {streaming && progress && (
          <div className="rounded-md border border-green-900/40 bg-green-950/20 px-2.5 py-2 text-xs">
            <div className="flex items-center gap-2">
              <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-green-400" />
              <span className="text-green-300">
                {progress.toolCalls > 0
                  ? `${progress.toolCalls} tool calls`
                  : "Starting..."}
              </span>
              {progress.lastTool && (
                <span className="font-mono text-zinc-500">
                  [{progress.lastTool}]
                </span>
              )}
              {onStop && (
                <button
                  type="button"
                  onClick={() => onStop(task.id)}
                  className="ml-auto rounded-md border border-red-800/60 bg-red-950/40 px-2 py-0.5 text-red-300 hover:border-red-700 hover:bg-red-900/40"
                >
                  Stop
                </button>
              )}
            </div>
            {progress.currentAction && (
              <div className="mt-1.5 truncate text-zinc-300">
                {progress.currentAction}
              </div>
            )}
          </div>
        )}

        {/* Persisted progress info (shown after stream ends) */}
        {!streaming && persistedProgress && persistedProgress.toolCalls > 0 && (
          <div className="rounded-md border border-zinc-700/40 bg-zinc-800/30 px-2.5 py-2 text-xs">
            <div className="flex items-center gap-2 text-zinc-400">
              <span>{persistedProgress.toolCalls} tool calls</span>
              {persistedProgress.lastTool && (
                <span className="font-mono text-zinc-500">
                  [{persistedProgress.lastTool}]
                </span>
              )}
            </div>
            {persistedProgress.summary && (
              <div className="mt-1.5 whitespace-pre-wrap text-zinc-300">
                {persistedProgress.summary.length > 500
                  ? persistedProgress.summary.slice(0, 500) + "..."
                  : persistedProgress.summary}
              </div>
            )}
          </div>
        )}

        {task.status === "failed" && task.error && (
          <div className="rounded-md border border-red-900/60 bg-red-950/30 px-2.5 py-1.5 text-xs">
            <span className="font-medium text-red-300">Failed:</span>{" "}
            <span className="whitespace-pre-wrap break-words text-red-200/90">
              {task.error}
            </span>
          </div>
        )}

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
                  Run AI review
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

        {(streaming || showActivity || showDiff || showReview) && (
          <div className="flex flex-wrap gap-2">
            {streaming && (
              <button
                type="button"
                onClick={() => onOpenPanel("live", task.id)}
                className={`flex items-center gap-1.5 rounded-md border px-2 py-1 text-xs transition-colors ${
                  activeKind === "live"
                    ? "border-green-700 bg-green-950/40 text-green-300"
                    : "border-zinc-800 bg-zinc-900/40 text-zinc-300 hover:border-zinc-700"
                }`}
              >
                <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-green-400" />
                Live
              </button>
            )}
            {showActivity && (
              <PanelChip
                label="Activity"
                active={activeKind === "activity"}
                onClick={() => onOpenPanel("activity", task.id)}
              />
            )}
            {showDiff && (
              <PanelChip
                label="Diff"
                active={activeKind === "diff"}
                onClick={() => onOpenPanel("diff", task.id)}
              />
            )}
            {showReview && (
              <PanelChip
                label="AI Review"
                active={activeKind === "review"}
                onClick={() => onOpenPanel("review", task.id)}
              />
            )}
          </div>
        )}
      </div>
    </div>
  );
}

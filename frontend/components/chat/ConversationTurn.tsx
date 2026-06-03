"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { type Task, type Event as ApiEvent } from "@/lib/api";
import { Badge, Button } from "@/components/ui";
import type { PanelKind } from "@/components/chat/RightPanel";

// One line of the agent's activity transcript, derived from a streamed event.
export type FeedItem = { kind: "message" | "thinking" | "tool" | "error"; text: string };

const FEED_ICON: Record<FeedItem["kind"], string> = {
  message: "💬",
  thinking: "💭",
  tool: "🔧",
  error: "⚠️",
};

// Flatten streamed events into a readable, ordered transcript: the agent's
// narration, reasoning, tool actions, and errors. Noise (meta/cost/tool_result)
// is dropped; `final` is shown separately as the summary.
export function buildActivityFeed(events: ApiEvent[]): FeedItem[] {
  const feed: FeedItem[] = [];
  const clip = (s: string, n: number) => (s.length > n ? s.slice(0, n - 1) + "…" : s);
  for (const ev of events) {
    let payload: { text?: string; data?: { name?: string; tool?: string }; tool?: string; name?: string } = {};
    try {
      payload = JSON.parse(ev.payload_json);
    } catch {
      payload = {};
    }
    const text = String(payload.text || "").trim();
    if (ev.type === "message") {
      if (text) feed.push({ kind: "message", text: clip(text, 600) });
    } else if (ev.type === "thinking") {
      const line = text.split("\n").map((s) => s.trim()).find(Boolean);
      if (line) feed.push({ kind: "thinking", text: clip(line, 240) });
    } else if (ev.type === "tool_use") {
      const name =
        payload.data?.name || payload.data?.tool || payload.tool || payload.name || text || "tool";
      feed.push({ kind: "tool", text: clip(String(name), 240) });
    } else if (ev.type === "error") {
      if (text) feed.push({ kind: "error", text: clip(text, 400) });
    }
  }
  return feed;
}

// The accumulating transcript. `live` keeps it auto-scrolled to the latest line
// and always expanded; otherwise it's a collapsible record of the finished run.
export function ActivityFeed({ feed, live }: { feed: FeedItem[]; live?: boolean }) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (live && ref.current) ref.current.scrollTop = ref.current.scrollHeight;
  }, [live, feed.length]);
  if (!feed.length) return null;

  const list = (
    <div ref={ref} className="max-h-60 space-y-1 overflow-y-auto pr-1">
      {feed.map((f, i) => (
        <div key={i} className="flex gap-1.5 whitespace-pre-wrap break-words">
          <span className="shrink-0">{FEED_ICON[f.kind]}</span>
          <span
            className={
              f.kind === "thinking"
                ? "text-muted-foreground"
                : f.kind === "error"
                  ? "text-red-300"
                  : f.kind === "tool"
                    ? "font-mono text-muted-foreground"
                    : "text-foreground"
            }
          >
            {f.text}
          </span>
        </div>
      ))}
    </div>
  );

  if (live) return <div className="mt-1.5">{list}</div>;
  return (
    <div className="mt-1.5">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="text-muted-foreground hover:text-foreground"
      >
        {open ? "▾" : "▸"} activity log ({feed.length})
      </button>
      {open && <div className="mt-1 border-l border-border pl-2">{list}</div>}
    </div>
  );
}

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
        const line = String(p.text || "")
          .split("\n")
          .map((s: string) => s.trim())
          .find(Boolean);
        if (line) {
          currentAction = line.length > 60 ? line.slice(0, 57) + "..." : line;
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
          ? "border-ring bg-muted text-foreground"
          : "border-border bg-card/40 text-muted-foreground hover:border-border hover:text-foreground"
      }`}
    >
      {label}
    </button>
  );
}

// Collapsible block showing persisted progress with expand/collapse for long summaries
function PersistedProgressBlock({
  progress,
}: {
  progress: { toolCalls: number; lastTool: string | null; summary: string | null; feed?: FeedItem[] };
}) {
  const [expanded, setExpanded] = useState(false);
  const PREVIEW_LENGTH = 300;
  const summaryTooLong =
    progress.summary && progress.summary.length > PREVIEW_LENGTH;

  return (
    <div className="rounded-md border border-border/40 bg-muted/30 px-2.5 py-2 text-xs">
      <div className="flex items-center gap-2 text-muted-foreground">
        <span>{progress.toolCalls} tool calls</span>
        {progress.lastTool && (
          <span className="font-mono text-muted-foreground">
            [{progress.lastTool}]
          </span>
        )}
        {summaryTooLong && (
          <button
            type="button"
            onClick={() => setExpanded((prev) => !prev)}
            className="ml-auto text-blue-400 hover:text-blue-300"
          >
            {expanded ? "Collapse" : "Expand"}
          </button>
        )}
      </div>
      {progress.summary && (
        <div className="mt-1.5 whitespace-pre-wrap text-foreground">
          {expanded || !summaryTooLong
            ? progress.summary
            : progress.summary.slice(0, PREVIEW_LENGTH) + "..."}
        </div>
      )}
      <ActivityFeed feed={progress.feed ?? []} />
    </div>
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
  feed?: FeedItem[];
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
  onCreateFollowUp,
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
  onCreateFollowUp?: (sourceTaskId: number) => void;
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
        <div className="max-w-[85%] rounded-lg rounded-br-sm bg-muted px-3 py-2 text-sm text-foreground">
          {task.parent_id && (
            <div className="mb-1 text-[10px] text-muted-foreground">
              ↳ subtask of #{task.parent_id}
            </div>
          )}
          <div className="whitespace-pre-wrap break-words">{task.title}</div>
          {task.description && (
            <div className="mt-1 whitespace-pre-wrap break-words text-xs text-muted-foreground">
              {task.description}
            </div>
          )}
        </div>
      </div>

      <div className="max-w-[92%] space-y-2 rounded-lg rounded-bl-sm border border-border bg-card/40 px-3 py-2">
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
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
                  className="rounded-sm bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground"
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
            className="ml-auto text-muted-foreground hover:text-foreground"
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
                <span className="font-mono text-muted-foreground">
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
            <ActivityFeed feed={buildActivityFeed(liveEvents)} live />
          </div>
        )}

        {/* Persisted progress info (shown after stream ends) */}
        {!streaming &&
          persistedProgress &&
          (persistedProgress.toolCalls > 0 ||
            (persistedProgress.feed?.length ?? 0) > 0) && (
          <PersistedProgressBlock progress={persistedProgress} />
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
                    : "border-border bg-card/40 text-foreground hover:border-border"
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
            {/* Show "Create follow-up" for merged/completed tasks */}
            {onCreateFollowUp && task.status === "merged" && (
              <button
                type="button"
                onClick={() => onCreateFollowUp(task.id)}
                className="rounded-md border border-blue-800/60 bg-blue-950/30 px-2 py-1 text-xs text-blue-300 transition-colors hover:border-blue-700 hover:bg-blue-900/40"
                title="Create a new task based on this one (inherits context)"
              >
                + Follow-up
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

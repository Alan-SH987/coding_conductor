"use client";

import Link from "next/link";
import { type Task } from "@/lib/api";
import { Badge, Button } from "@/components/ui";
import type { PanelKind } from "@/components/chat/RightPanel";

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
// prompt) and a left-aligned "assistant" block (status + inline actions). All
// verbose output (live stream, activity, diff, review) opens in the right-side
// drawer via onOpenPanel instead of expanding inline, keeping the column lean.
export function ConversationTurn({
  task,
  hasChildren,
  streaming,
  busy,
  activeKind,
  onRun,
  onReview,
  onApprove,
  onReject,
  onOpenPanel,
}: {
  task: Task;
  hasChildren: boolean;
  streaming: boolean;
  busy: boolean;
  activeKind: PanelKind | null;
  onRun: (id: number) => void;
  onReview: (id: number) => void;
  onApprove: (id: number) => void;
  onReject: (id: number) => void;
  onOpenPanel: (kind: PanelKind, taskId: number) => void;
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

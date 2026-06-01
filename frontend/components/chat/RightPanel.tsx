"use client";

import { useEffect, useRef, useState } from "react";
import {
  api,
  type Event as ApiEvent,
  type Review,
  type Run,
} from "@/lib/api";
import { Badge } from "@/components/ui";

export type PanelKind = "live" | "activity" | "diff" | "review";

export interface PanelState {
  kind: PanelKind;
  taskId: number;
}

interface RunWithEvents extends Run {
  events: ApiEvent[];
}

const PANEL_TITLE: Record<PanelKind, string> = {
  live: "Live",
  activity: "Activity",
  diff: "Diff",
  review: "AI Review",
};

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

// Render a unified diff with light per-line coloring. No syntax-highlighting
// dependency — just prefix-based classes, which reads well for diffs.
function DiffView({ diff }: { diff: string }) {
  if (!diff.trim()) {
    return <div className="text-xs text-zinc-500">No diff.</div>;
  }
  return (
    <pre className="overflow-x-auto text-xs leading-relaxed">
      {diff.split("\n").map((line, i) => (
        <div key={i} className={diffLineClass(line)}>
          {line || " "}
        </div>
      ))}
    </pre>
  );
}

function diffLineClass(line: string): string {
  if (line.startsWith("+++") || line.startsWith("---")) return "text-zinc-500";
  if (line.startsWith("@@")) return "text-cyan-300";
  if (line.startsWith("diff ") || line.startsWith("index ")) return "text-zinc-600";
  if (line.startsWith("+")) return "text-green-300";
  if (line.startsWith("-")) return "text-red-300";
  return "text-zinc-400";
}

function ActivityView({ runs }: { runs: RunWithEvents[] }) {
  if (runs.length === 0) {
    return <div className="text-xs text-zinc-600">No runs yet.</div>;
  }
  return (
    <div className="space-y-3">
      {runs.map((r) => (
        <div key={r.id}>
          <div className="mb-1 flex items-center justify-between text-[10px] text-zinc-500">
            <span>
              run #{r.id} · {r.agent}
            </span>
            <span>
              {r.tokens_in}/{r.tokens_out} tok · ${r.cost.toFixed(4)}
            </span>
          </div>
          <EventLog events={r.events} />
        </div>
      ))}
    </div>
  );
}

function ReviewView({ review }: { review: Review | null }) {
  if (review === null) {
    return <div className="text-xs text-zinc-600">No review yet.</div>;
  }
  return (
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
  );
}

// Lazy-fetch the body for the non-live panels. Mounted with a key that includes
// kind/taskId/reloadNonce, so any of those changing remounts this with a fresh
// fetch — no stale data leaks across panel switches or reloads.
function FetchedBody({
  kind,
  taskId,
}: {
  kind: Exclude<PanelKind, "live">;
  taskId: number;
}) {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [data, setData] = useState<unknown>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        let result: unknown;
        if (kind === "diff") {
          result = (await api.getDiff(taskId)).diff;
        } else if (kind === "review") {
          result = await api.getReview(taskId);
        } else {
          const runs = await api.listRuns(taskId);
          result = await Promise.all(
            runs.map(async (r) => ({
              ...r,
              events: await api.listEvents(r.id),
            })),
          );
        }
        if (!cancelled) setData(result);
      } catch (e) {
        if (!cancelled) setError(String(e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [kind, taskId]);

  if (loading) return <div className="text-xs text-zinc-500">loading…</div>;
  if (error) return <div className="text-xs text-red-400">{error}</div>;
  if (kind === "diff") return <DiffView diff={data as string} />;
  if (kind === "review") return <ReviewView review={data as Review | null} />;
  return <ActivityView runs={data as RunWithEvents[]} />;
}

// A slide-out drawer overlaying the conversation column. Keeps the chat concise
// by hosting all verbose output (live stream, activity, diff, review) here — the
// way you'd pop open a file to the side rather than scrolling it inline.
export function RightPanel({
  panel,
  liveEvents,
  streaming,
  reloadNonce,
  onClose,
}: {
  panel: PanelState | null;
  liveEvents: ApiEvent[];
  streaming: boolean;
  reloadNonce: number;
  onClose: () => void;
}) {
  const open = panel !== null;
  const scrollRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(480); // 30rem = 480px
  const [isResizing, setIsResizing] = useState(false);

  // Auto-scroll to bottom when liveEvents updates
  useEffect(() => {
    if (panel?.kind === "live" && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [liveEvents, panel?.kind]);

  // Handle resizing
  useEffect(() => {
    if (!isResizing) return;

    const handleMouseMove = (e: MouseEvent) => {
      const newWidth = window.innerWidth - e.clientX;
      setWidth(Math.max(300, Math.min(newWidth, window.innerWidth * 0.85)));
    };

    const handleMouseUp = () => {
      setIsResizing(false);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };

    // Prevent text selection during resize
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";

    document.addEventListener("mousemove", handleMouseMove);
    document.addEventListener("mouseup", handleMouseUp);

    return () => {
      document.removeEventListener("mousemove", handleMouseMove);
      document.removeEventListener("mouseup", handleMouseUp);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
  }, [isResizing]);

  return (
    <div
      style={{ width: open ? `${width}px` : undefined }}
      className={`absolute inset-y-0 right-0 z-20 flex flex-col border-l border-zinc-800 bg-zinc-950 shadow-2xl transition-transform duration-200 ${
        open ? "translate-x-0" : "pointer-events-none translate-x-full"
      }`}
    >
      {/* Resize handle */}
      {open && (
        <div
          onMouseDown={(e) => {
            e.preventDefault();
            setIsResizing(true);
          }}
          className={`absolute left-0 top-0 h-full w-1.5 cursor-col-resize transition-colors ${
            isResizing ? "bg-blue-500" : "bg-transparent hover:bg-zinc-600"
          }`}
        />
      )}
      {panel && (
        <>
          <div className="flex items-center justify-between border-b border-zinc-800 px-3 py-2">
            <div className="flex items-center gap-2 text-sm font-medium text-zinc-200">
              {PANEL_TITLE[panel.kind]}
              <span className="font-mono text-xs text-zinc-500">
                #{panel.taskId}
              </span>
            </div>
            <button
              type="button"
              onClick={onClose}
              aria-label="Close panel"
              className="rounded p-1 text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200"
            >
              ✕
            </button>
          </div>
          <div ref={scrollRef} className="flex-1 overflow-y-auto px-3 py-3">
            {panel.kind === "live" ? (
              <>
                <div className="mb-2 flex items-center gap-2 text-[10px] text-zinc-500">
                  {streaming ? (
                    <>
                      <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-green-400" />
                      live
                    </>
                  ) : (
                    "stream ended"
                  )}
                </div>
                {liveEvents.length === 0 ? (
                  <div className="text-xs text-zinc-600">
                    waiting for first event…
                  </div>
                ) : (
                  <EventLog events={liveEvents} />
                )}
              </>
            ) : (
              <FetchedBody
                key={`${panel.kind}:${panel.taskId}:${reloadNonce}`}
                kind={panel.kind}
                taskId={panel.taskId}
              />
            )}
          </div>
        </>
      )}
    </div>
  );
}

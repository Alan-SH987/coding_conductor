"use client";

import { useState, useEffect } from "react";
import Link from "next/link";
import { api, type Task } from "@/lib/api";
import { Button, Badge } from "./ui";

interface NextStepsModalProps {
  taskId: number;
  onClose: () => void;
  onTasksSelected: (taskIds: number[]) => void;
}

export function NextStepsModal({ taskId, onClose, onTasksSelected }: NextStepsModalProps) {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [parent, setParent] = useState<Task | null>(null);
  const [pendingTasks, setPendingTasks] = useState<Task[]>([]);
  const [siblingTasks, setSiblingTasks] = useState<Task[]>([]);
  const [selectedTaskIds, setSelectedTaskIds] = useState<number[]>([]);

  useEffect(() => {
    async function loadNextSteps() {
      try {
        setLoading(true);
        const data = await api.getNextSteps(taskId);
        setParent(data.parent);
        setPendingTasks(data.pending_tasks);
        setSiblingTasks(data.sibling_tasks);
        setError(null);
      } catch (err) {
        setError(String(err));
      } finally {
        setLoading(false);
      }
    }
    loadNextSteps();
  }, [taskId]);

  const toggleTaskSelection = (id: number) => {
    setSelectedTaskIds((prev) =>
      prev.includes(id) ? prev.filter((tid) => tid !== id) : [...prev, id]
    );
  };

  const handleContinue = async () => {
    if (selectedTaskIds.length === 0) {
      return;
    }

    try {
      const allTaskIds = [...pendingTasks, ...siblingTasks].map((t) => t.id);
      await api.saveNextStepsChoice(taskId, allTaskIds, selectedTaskIds, "selected");
      onTasksSelected(selectedTaskIds);
      onClose();
    } catch (err) {
      setError(String(err));
    }
  };

  const handleSkip = async () => {
    try {
      const allTaskIds = [...pendingTasks, ...siblingTasks].map((t) => t.id);
      await api.saveNextStepsChoice(taskId, allTaskIds, [], "skipped");
      onClose();
    } catch (err) {
      setError(String(err));
    }
  };

  if (loading) {
    return (
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/80">
        <div className="w-full max-w-2xl rounded-lg border border-border bg-card p-6">
          <div className="text-center text-muted-foreground">Loading next steps...</div>
        </div>
      </div>
    );
  }

  const hasAnyTasks = pendingTasks.length > 0 || siblingTasks.length > 0;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/80">
      <div className="w-full max-w-2xl rounded-lg border border-border bg-card p-6">
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-xl font-semibold text-foreground">What's Next?</h2>
          <button
            onClick={onClose}
            className="text-muted-foreground hover:text-foreground"
            aria-label="Close"
          >
            ✕
          </button>
        </div>

        {error && (
          <div className="mb-4 rounded border border-red-800 bg-red-900/20 p-3 text-sm text-red-200">
            {error}
          </div>
        )}

        {!hasAnyTasks && (
          <div className="mb-6 text-center text-muted-foreground">
            <p className="mb-4">No pending tasks found in this project.</p>
            {parent && (
              <Link href={`/tasks/${parent.id}`}>
                <Button variant="ghost" className="mb-2">
                  ← Back to Parent Task
                </Button>
              </Link>
            )}
          </div>
        )}

        {siblingTasks.length > 0 && (
          <div className="mb-6">
            <h3 className="mb-3 text-sm font-medium text-foreground">
              Related Subtasks ({siblingTasks.length})
            </h3>
            <div className="space-y-2">
              {siblingTasks.map((task) => (
                <TaskCard
                  key={task.id}
                  task={task}
                  selected={selectedTaskIds.includes(task.id)}
                  onToggle={() => toggleTaskSelection(task.id)}
                />
              ))}
            </div>
          </div>
        )}

        {pendingTasks.length > 0 && (
          <div className="mb-6">
            <h3 className="mb-3 text-sm font-medium text-foreground">
              Other Pending Tasks ({pendingTasks.length})
            </h3>
            <div className="space-y-2">
              {pendingTasks.map((task) => (
                <TaskCard
                  key={task.id}
                  task={task}
                  selected={selectedTaskIds.includes(task.id)}
                  onToggle={() => toggleTaskSelection(task.id)}
                />
              ))}
            </div>
          </div>
        )}

        <div className="flex items-center justify-between border-t border-border pt-4">
          <div className="text-sm text-muted-foreground">
            {selectedTaskIds.length > 0
              ? `${selectedTaskIds.length} task${selectedTaskIds.length > 1 ? "s" : ""} selected`
              : "Select tasks to work on next"}
          </div>
          <div className="flex gap-2">
            <Button variant="ghost" onClick={handleSkip}>
              Skip
            </Button>
            {selectedTaskIds.length > 0 && (
              <Button onClick={handleContinue}>
                {selectedTaskIds.length === 1
                  ? "Start Task"
                  : `Start ${selectedTaskIds.length} Tasks`}
              </Button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

interface TaskCardProps {
  task: Task;
  selected: boolean;
  onToggle: () => void;
}

function TaskCard({ task, selected, onToggle }: TaskCardProps) {
  return (
    <div
      className={`cursor-pointer rounded border p-3 transition ${
        selected
          ? "border-blue-600 bg-blue-900/20"
          : "border-border bg-card/50 hover:border-border"
      }`}
      onClick={onToggle}
    >
      <div className="flex items-start justify-between">
        <div className="flex-1">
          <div className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={selected}
              onChange={onToggle}
              onClick={(e) => e.stopPropagation()}
              className="rounded border-border"
            />
            <Link
              href={`/tasks/${task.id}`}
              onClick={(e) => e.stopPropagation()}
              className="font-medium text-foreground hover:text-blue-400"
            >
              {task.title}
            </Link>
            <Badge status={task.status} />
          </div>
          {task.description && (
            <p className="ml-6 mt-1 text-sm text-muted-foreground line-clamp-2">
              {task.description}
            </p>
          )}
        </div>
      </div>
    </div>
  );
}

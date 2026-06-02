// Typed client for the Coding Conductor backend (FastAPI on :8000).
// All calls are no-store; this is a live control console, not a cacheable site.

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

// ---------- domain types (mirror backend/app/storage/models.py) ----------
export interface Project {
  id: number;
  name: string;
  path: string;
  default_branch: string;
  is_pinned: boolean;
  is_archived: boolean;
  deleted_at: string | null;
  created_at: string;
  verify_cmd: string | null;
  enabled_skills: string | null;
}

export interface Skill {
  name: string;
  description: string;
}

export type TaskStatus =
  | "draft"
  | "planning"
  | "planned"
  | "queued"
  | "running"
  | "review"
  | "awaiting_approval"
  | "merged"
  | "rejected"
  | "failed";

export interface Task {
  id: number;
  project_id: number;
  parent_id: number | null;
  title: string;
  description: string;
  status: TaskStatus;
  assigned_agent: string | null;
  branch: string | null;
  worktree_path: string | null;
  error: string | null;
  tags: string | null; // JSON array of tags, e.g. '["#auth", "#api"]'
  created_at: string;
  updated_at: string;
}

export interface Run {
  id: number;
  task_id: number;
  agent: string;
  session_id: string | null;
  status: "running" | "succeeded" | "failed";
  tokens_in: number;
  tokens_out: number;
  cost: number;
  duration_ms: number;
  diff_ref: string | null;
  started_at: string;
  ended_at: string | null;
}

export interface Event {
  id: number;
  run_id: number;
  seq: number;
  type: string;
  payload_json: string;
  ts: string;
}

export interface Agent {
  name: string;
  capabilities: string[];
}

export type AgentStatus =
  | "available"
  | "unauthenticated"
  | "rate_limited"
  | "unavailable";

export interface AgentHealth {
  name: string;
  ok: boolean;
  auth_ok: boolean;
  rate_limited: boolean;
  version: string;
  detail: string;
  status: AgentStatus;
}

export interface ApproveResult {
  ok: boolean;
  merged_sha: string | null;
  conflict: boolean;
  conflicted_files: string[];
  verify_failed: boolean;
  verify_output: string;
  dirty: boolean;
  dirty_files: string[];
  task: Task;
}

export type Verdict = "approve" | "request_changes";

export interface ReviewFinding {
  severity: string; // blocker | warning | nit
  comment: string;
  file?: string;
}

export interface Review {
  id: number;
  task_id: number;
  run_id: number | null;
  agent: string;
  verdict: Verdict;
  summary: string;
  findings: ReviewFinding[];
  created_at: string;
}

export interface NextStepsResponse {
  task: Task;
  parent: Task | null;
  pending_tasks: Task[];
  sibling_tasks: Task[];
}

export interface TaskSuggestion {
  id: number;
  task_id: number;
  suggested_task_ids: string;
  selected_task_ids: string;
  action_taken: string | null;
  created_at: string;
}

// ---------- transport ----------
async function http<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    cache: "no-store",
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? body.error ?? detail;
    } catch {
      // non-JSON error body; keep statusText
    }
    throw new Error(`${res.status}: ${detail}`);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

const post = <T>(path: string, body?: unknown) =>
  http<T>(path, { method: "POST", body: body ? JSON.stringify(body) : undefined });

const del = <T>(path: string) =>
  http<T>(path, { method: "DELETE" });

const patch = <T>(path: string, body?: unknown) =>
  http<T>(path, { method: "PATCH", body: body ? JSON.stringify(body) : undefined });

// ---------- endpoints ----------
export const api = {
  listAgents: () => http<Agent[]>("/agents"),
  checkAgentsHealth: () => http<AgentHealth[]>("/agents/health"),

  listProjects: (includeArchived = false) =>
    http<Project[]>(`/projects?include_archived=${includeArchived}`),
  getProject: (id: number) => http<Project>(`/projects/${id}`),
  createProject: (name: string, path: string, init = true) =>
    post<Project>("/projects", { name, path, init }),
  pinProject: (id: number) => post<Project>(`/projects/${id}/pin`),
  unpinProject: (id: number) => post<Project>(`/projects/${id}/unpin`),
  archiveProject: (id: number) => post<Project>(`/projects/${id}/archive`),
  unarchiveProject: (id: number) => post<Project>(`/projects/${id}/unarchive`),
  deleteProject: (id: number) => del<Project>(`/projects/${id}`),
  updateProjectVerify: (id: number, verifyCmd: string | null) =>
    patch<Project>(`/projects/${id}/verify`, { verify_cmd: verifyCmd }),
  listSkills: () => http<Skill[]>("/skills"),
  updateProjectSkills: (id: number, enabled: string[]) =>
    patch<Project>(`/projects/${id}/skills`, { enabled }),
  distillProject: (id: number) =>
    post<{ insights: string }>(`/projects/${id}/distill`),

  listTasks: (projectId: number) =>
    http<Task[]>(`/projects/${projectId}/tasks`),
  createTask: (
    projectId: number,
    title: string,
    description: string,
    agent: string,
  ) => post<Task>(`/projects/${projectId}/tasks`, { title, description, agent }),

  getTask: (id: number) => http<Task>(`/tasks/${id}`),
  runTask: (id: number) => post<Task>(`/tasks/${id}/run`),
  planTask: (id: number) => post<Task[]>(`/tasks/${id}/plan`),
  streamUrl: (id: number) => `${API_BASE}/tasks/${id}/stream`,
  getDiff: (id: number) =>
    http<{ task_id: number; diff: string }>(`/tasks/${id}/diff`),
  reviewTask: (id: number) => post<Review>(`/tasks/${id}/review`),
  getReview: (id: number) => http<Review | null>(`/tasks/${id}/review`),
  reviseTask: (id: number) => post<Task>(`/tasks/${id}/revise`),
  approve: (id: number) => post<ApproveResult>(`/tasks/${id}/approve`),
  reject: (id: number) => post<Task>(`/tasks/${id}/reject`),

  listRuns: (taskId: number) => http<Run[]>(`/tasks/${taskId}/runs`),
  listEvents: (runId: number) => http<Event[]>(`/runs/${runId}/events`),

  getNextSteps: (taskId: number) => http<NextStepsResponse>(`/tasks/${taskId}/next-steps`),
  saveNextStepsChoice: (
    taskId: number,
    suggestedTaskIds: number[],
    selectedTaskIds: number[],
    action: string,
  ) =>
    post<TaskSuggestion>(`/tasks/${taskId}/next-steps`, {
      suggested_task_ids: suggestedTaskIds,
      selected_task_ids: selectedTaskIds,
      action,
    }),
};

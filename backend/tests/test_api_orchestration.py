"""HTTP-level tests for the orchestration API.

The Orchestrator dependency is overridden with one backed by a `FakeAdapter`
(no login/network) and a throwaway SQLite engine. The TestClient is used
without its context manager on purpose, so app lifespan (which would touch the
real default DB) never fires.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import AsyncIterator

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, create_engine

from app.adapters.base import (
    AgentAdapter,
    AgentEvent,
    EventType,
    HealthStatus,
    ReviewFinding,
    ReviewResult,
    RunContext,
    SubtaskSpec,
    TaskSpec,
)
from app.api.deps import get_orchestrator
from app.main import app
from app.orchestrator import Orchestrator


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True, text=True)


def _init_repo(path: Path) -> None:
    _git(["init", "-b", "main"], path)
    _git(["config", "user.name", "Test"], path)
    _git(["config", "user.email", "test@example.com"], path)
    (path / "README.md").write_text("# test repo\n")
    _git(["add", "-A"], path)
    _git(["commit", "-m", "init"], path)


class FakeAdapter(AgentAdapter):
    name = "claude"  # registered under the default agent key
    capabilities = {"plan", "code", "review"}

    def __init__(self):
        self.seen_system_prompts: list[str] = []  # one per run(), for assertions
        self.seen_goals: list[str] = []

    async def plan(self, goal, repo_path, capabilities) -> list[SubtaskSpec]:
        return [
            SubtaskSpec(title="sub a", description="do a", capability="code"),
            SubtaskSpec(title="sub b", description="do b", capability="code"),
        ]

    async def review(self, goal, diff, repo_path) -> ReviewResult:
        return ReviewResult(
            verdict="request_changes",
            summary="needs a tweak",
            findings=[ReviewFinding(
                severity="warning", comment="add a test", file="hello.txt")],
        )

    async def run(self, spec: TaskSpec, ctx: RunContext) -> AsyncIterator[AgentEvent]:
        self.seen_system_prompts.append(ctx.system_prompt)
        self.seen_goals.append(spec.goal)
        yield AgentEvent(EventType.meta, data={"session_id": "api-sess"})
        yield AgentEvent(EventType.message, text="working")
        (Path(ctx.worktree_path) / "hello.txt").write_text("hello from fake adapter\n")
        yield AgentEvent(EventType.tool_use, text="Write", data={"name": "Write"})
        yield AgentEvent(EventType.final, text="done", data={"session_id": "api-sess"})
        yield AgentEvent(EventType.cost, data={
            "cost_usd": 0.02, "input_tokens": 10, "output_tokens": 5, "duration_ms": 99,
        })

    async def healthcheck(self) -> HealthStatus:
        return HealthStatus(ok=True, auth_ok=True)


@pytest.fixture
def orch(tmp_path):
    db = tmp_path / "api.db"
    eng = create_engine(f"sqlite:///{db}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(eng)
    return Orchestrator({"claude": FakeAdapter()}, engine=eng)


@pytest.fixture
def overridden(orch):
    app.dependency_overrides[get_orchestrator] = lambda: orch
    yield orch
    app.dependency_overrides.clear()


@pytest.fixture
def client(overridden):
    return TestClient(app)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _init_repo(r)
    return r


def _make_project_and_task(client, repo) -> tuple[int, int]:
    pid = client.post("/projects", json={"name": "p", "path": str(repo)}).json()["id"]
    tid = client.post(f"/projects/{pid}/tasks", json={"title": "do thing"}).json()["id"]
    return pid, tid


# --------------------------------------------------------------------------
# happy path — async run (202) driven to completion through the SSE stream
#
# POST /run now returns 202 and drives the agent in a detached asyncio task, so
# the flow can't be observed by a single blocking call. We run everything on one
# event loop via httpx.ASGITransport (which does NOT fire lifespan, keeping the
# default DB untouched): the background run and the SSE consumer share the loop,
# so tailing the stream to its terminal `done` frame also drives the run.
# --------------------------------------------------------------------------
async def _drain_stream(ac: httpx.AsyncClient, tid: int):
    """Consume /tasks/{tid}/stream, returning (event_types, done_payload)."""
    ev_types: list[str] = []
    done = None
    cur: str | None = None
    async with ac.stream("GET", f"/tasks/{tid}/stream") as resp:
        assert resp.status_code == 200
        async for line in resp.aiter_lines():
            if line.startswith("event: "):
                cur = line[len("event: "):]
            elif line.startswith("data: "):
                payload = json.loads(line[len("data: "):])
                if cur == "ev":
                    ev_types.append(payload["type"])
                elif cur == "done":
                    done = payload
                    break
    return ev_types, done


async def _run_and_drain(ac: httpx.AsyncClient, repo_path: str) -> int:
    """Create project+task, fire the async run, and tail it to awaiting_approval."""
    pid = (await ac.post("/projects", json={"name": "p", "path": repo_path})).json()["id"]
    tid = (await ac.post(f"/projects/{pid}/tasks", json={"title": "do thing"})).json()["id"]

    r = await ac.post(f"/tasks/{tid}/run")
    assert r.status_code == 202
    assert r.json()["status"] == "running"
    assert r.json()["branch"] is None  # branch is assigned by the background run

    ev_types, done = await _drain_stream(ac, tid)
    assert ev_types == ["meta", "message", "tool_use", "final", "cost"]
    assert done == {"status": "awaiting_approval", "run_status": "succeeded"}
    return tid


def _asgi_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def test_full_flow_run_stream_approve(overridden, repo):
    async def flow():
        async with _asgi_client() as ac:
            tid = await _run_and_drain(ac, str(repo))

            r = await ac.get(f"/tasks/{tid}/diff")
            assert r.status_code == 200
            assert "hello from fake adapter" in r.json()["diff"]

            runs = (await ac.get(f"/tasks/{tid}/runs")).json()
            assert len(runs) == 1 and runs[0]["status"] == "succeeded"
            assert runs[0]["session_id"] == "api-sess"

            events = (await ac.get(f"/runs/{runs[0]['id']}/events")).json()
            assert [e["type"] for e in events] == \
                ["meta", "message", "tool_use", "final", "cost"]

            body = (await ac.post(f"/tasks/{tid}/approve")).json()
            assert body["ok"] is True
            assert body["merged_sha"]
            assert body["task"]["status"] == "merged"
            assert body["task"]["branch"] == f"conductor/task-{tid}"

    asyncio.run(asyncio.wait_for(flow(), timeout=15))
    assert (repo / "hello.txt").read_text() == "hello from fake adapter\n"


def test_reject_flow_discards(overridden, repo):
    async def flow():
        async with _asgi_client() as ac:
            tid = await _run_and_drain(ac, str(repo))
            r = await ac.post(f"/tasks/{tid}/reject")
            assert r.status_code == 200
            assert r.json()["status"] == "rejected"

    asyncio.run(asyncio.wait_for(flow(), timeout=15))
    assert not (repo / "hello.txt").exists()


def test_review_is_advisory(overridden, repo):
    async def flow():
        async with _asgi_client() as ac:
            tid = await _run_and_drain(ac, str(repo))

            # no review yet
            assert (await ac.get(f"/tasks/{tid}/review")).json() is None

            r = await ac.post(f"/tasks/{tid}/review")
            assert r.status_code == 200
            body = r.json()
            assert body["verdict"] == "request_changes"
            assert body["agent"] == "claude"
            assert body["summary"] == "needs a tweak"
            assert body["findings"] == [
                {"severity": "warning", "comment": "add a test", "file": "hello.txt"}
            ]

            # GET returns the latest persisted review
            got = (await ac.get(f"/tasks/{tid}/review")).json()
            assert got["id"] == body["id"] and got["verdict"] == "request_changes"

            # an advisory review must not move the task off the approval gate
            assert (await ac.get(f"/tasks/{tid}")).json()["status"] == "awaiting_approval"

    asyncio.run(asyncio.wait_for(flow(), timeout=15))


# --------------------------------------------------------------------------
# revise / handoff loop (Bundle A) — re-run in the same worktree to address a
# request_changes review; the prior review then goes stale (hidden) until a
# fresh review runs against the new run.
# --------------------------------------------------------------------------
def test_revise_addresses_review_and_stales_it(overridden, repo):
    async def flow():
        async with _asgi_client() as ac:
            tid = await _run_and_drain(ac, str(repo))

            rev = (await ac.post(f"/tasks/{tid}/review")).json()
            assert rev["verdict"] == "request_changes"
            assert (await ac.get(f"/tasks/{tid}/review")).json()["id"] == rev["id"]

            # revise: re-run in the same worktree, addressing the review
            r = await ac.post(f"/tasks/{tid}/revise")
            assert r.status_code == 202
            assert r.json()["status"] == "running"
            ev_types, done = await _drain_stream(ac, tid)
            assert ev_types == ["meta", "message", "tool_use", "final", "cost"]
            assert done == {"status": "awaiting_approval", "run_status": "succeeded"}

            # a second run exists; the old review is now stale (run-mismatched)
            runs = (await ac.get(f"/tasks/{tid}/runs")).json()
            assert len(runs) == 2
            assert (await ac.get(f"/tasks/{tid}/review")).json() is None
            assert (await ac.get(f"/tasks/{tid}")).json()["status"] == "awaiting_approval"

    asyncio.run(asyncio.wait_for(flow(), timeout=15))
    # the review's finding was injected into the revision run's system prompt
    prompts = overridden.adapters["claude"].seen_system_prompts
    assert len(prompts) == 2
    assert "add a test" in prompts[-1]
    assert "add a test" not in prompts[0]


def test_revise_requires_review(overridden, repo):
    # at the approval gate but with no review yet → 409
    async def flow():
        async with _asgi_client() as ac:
            tid = await _run_and_drain(ac, str(repo))
            r = await ac.post(f"/tasks/{tid}/revise")
            assert r.status_code == 409

    asyncio.run(asyncio.wait_for(flow(), timeout=15))


def test_compose_revision_prompt_includes_findings():
    prompt = Orchestrator._compose_revision_prompt(
        "needs work",
        [{"severity": "blocker", "file": "a.py", "comment": "fix the bug"},
         {"severity": "nit", "comment": "rename x"},
         {"comment": ""}],  # dropped: empty comment
    )
    assert "needs work" in prompt
    assert "[blocker] a.py: fix the bug" in prompt
    assert "[nit] rename x" in prompt


def test_list_projects_and_tasks(client, repo):
    pid, tid = _make_project_and_task(client, repo)

    projects = client.get("/projects").json()
    assert any(p["id"] == pid for p in projects)

    tasks = client.get(f"/projects/{pid}/tasks").json()
    assert [t["id"] for t in tasks] == [tid]


def test_run_rejects_over_budget_before_starting(client, repo):
    pid, tid = _make_project_and_task(client, repo)

    r = client.patch(
        f"/projects/{pid}/quotas",
        json={"quota_tokens": 0, "quota_cost_usd": None},
    )
    assert r.status_code == 200

    r = client.post(f"/tasks/{tid}/run")
    assert r.status_code == 429
    assert "Token quota exceeded" in r.json()["detail"]
    assert client.get(f"/tasks/{tid}").json()["status"] == "draft"


# --------------------------------------------------------------------------
# planner / router
# --------------------------------------------------------------------------
def test_plan_creates_routed_subtasks(client, repo):
    pid, tid = _make_project_and_task(client, repo)

    r = client.post(f"/tasks/{tid}/plan")
    assert r.status_code == 201
    kids = r.json()
    assert [k["title"] for k in kids] == ["sub a", "sub b"]
    assert all(k["parent_id"] == tid for k in kids)
    assert all(k["status"] == "draft" for k in kids)
    # only adapter capable of "code" is the fake claude, so the router picks it
    assert all(k["assigned_agent"] == "claude" for k in kids)

    # parent becomes a non-runnable container
    assert client.get(f"/tasks/{tid}").json()["status"] == "planned"
    assert client.post(f"/tasks/{tid}/plan").status_code == 409  # re-plan blocked
    assert client.post(f"/tasks/{tid}/run").status_code == 409   # container, not runnable

    # children are listed under the project alongside their parent
    listed = client.get(f"/projects/{pid}/tasks").json()
    assert {k["id"] for k in kids}.issubset({t["id"] for t in listed})


# --------------------------------------------------------------------------
# error mapping
# --------------------------------------------------------------------------
def test_unknown_task_404(client):
    assert client.get("/tasks/9999").status_code == 404
    assert client.post("/tasks/9999/run").status_code == 404
    assert client.post("/tasks/9999/plan").status_code == 404
    assert client.get("/tasks/9999/diff").status_code == 404
    assert client.post("/tasks/9999/review").status_code == 404
    assert client.get("/tasks/9999/review").status_code == 404
    assert client.post("/tasks/9999/revise").status_code == 404
    assert client.post("/tasks/9999/approve").status_code == 404
    assert client.post("/tasks/9999/reject").status_code == 404


def test_unknown_project_404(client):
    assert client.get("/projects/9999").status_code == 404
    assert client.post("/projects/9999/tasks", json={"title": "t"}).status_code == 404


def test_bad_agent_400(client, repo):
    pid = client.post("/projects", json={"name": "p", "path": str(repo)}).json()["id"]
    r = client.post(f"/projects/{pid}/tasks", json={"title": "t", "agent": "ghost"})
    assert r.status_code == 400


def test_list_agents(client):
    agents = client.get("/agents").json()
    names = {a["name"] for a in agents}
    # Should include "auto" (intelligent routing) and "claude"
    assert "auto" in names
    assert "claude" in names
    # Find the claude agent and check its capabilities
    claude = next(a for a in agents if a["name"] == "claude")
    assert claude["capabilities"] == ["code", "plan", "review"]


def test_non_git_path_auto_inits(client, tmp_path):
    """By default an empty/non-git path is auto-initialized into a git project."""
    plain = tmp_path / "plain"
    plain.mkdir()
    r = client.post("/projects", json={"name": "p", "path": str(plain)})
    assert r.status_code == 201
    assert (plain / ".git").is_dir()  # auto `git init` happened


def test_non_git_path_rejected_when_init_false(client, tmp_path):
    """With init=false the old behavior holds: a non-git path is rejected."""
    plain = tmp_path / "plain2"
    plain.mkdir()
    r = client.post("/projects", json={"name": "p", "path": str(plain), "init": False})
    assert r.status_code == 400
    assert r.json()["error"] == "not_a_git_repo"


def test_agents_health_status_derivation(tmp_path):
    """GET /agents/health maps each adapter's HealthStatus to a derived status."""

    class _HC(FakeAdapter):
        def __init__(self, hs: HealthStatus):
            super().__init__()
            self._hs = hs

        async def healthcheck(self) -> HealthStatus:
            return self._hs

    db = tmp_path / "health.db"
    eng = create_engine(f"sqlite:///{db}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(eng)
    adapters = {
        "claude": _HC(HealthStatus(ok=True, auth_ok=True)),
        "codex": _HC(HealthStatus(ok=True, auth_ok=True, rate_limited=True)),
        "noauth": _HC(HealthStatus(ok=True, auth_ok=False)),
        "down": _HC(HealthStatus(ok=False, auth_ok=False, detail="CLI not found")),
    }
    app.dependency_overrides[get_orchestrator] = lambda: Orchestrator(adapters, engine=eng)
    try:
        res = TestClient(app).get("/agents/health")
        assert res.status_code == 200
        by_name = {a["name"]: a for a in res.json()}
        assert by_name["claude"]["status"] == "available"
        assert by_name["codex"]["status"] == "rate_limited"
        assert by_name["codex"]["rate_limited"] is True
        assert by_name["noauth"]["status"] == "unauthenticated"
        assert by_name["down"]["status"] == "unavailable"
    finally:
        app.dependency_overrides.clear()


def test_reconcile_orphaned_running_tasks(orch):
    """A task left 'running' by a restart is reconciled to failed with a reason."""
    from sqlmodel import Session

    from app.storage import models

    with Session(orch.engine) as s:
        p = models.Project(name="p", path="/tmp/whatever", default_branch="main")
        s.add(p)
        s.commit()
        s.refresh(p)
        t = models.Task(project_id=p.id, title="stuck", status=models.TaskStatus.running.value)
        s.add(t)
        s.commit()
        s.refresh(t)
        tid = t.id

    assert orch.reconcile_orphaned_runs() == 1
    task = orch.get_task(tid)
    assert task.status == "failed"
    assert "interrupted" in (task.error or "")


def test_run_records_failure_reason(repo, tmp_path):
    """A run that errors records WHY on the task instead of failing silently."""
    import asyncio

    from sqlmodel import SQLModel, create_engine

    class ErrAdapter(FakeAdapter):
        async def run(self, spec, ctx):
            yield AgentEvent(EventType.meta, data={"session_id": "x"})
            yield AgentEvent(EventType.error, text="boom: the agent blew up",
                             data={"kind": "runtime"})

    db = tmp_path / "err.db"
    eng = create_engine(f"sqlite:///{db}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(eng)
    orch = Orchestrator({"claude": ErrAdapter()}, engine=eng)
    proj = orch.create_project("p", str(repo))
    task = orch.create_task(proj.id, "do thing")

    asyncio.run(orch.run_task(task.id))

    t = orch.get_task(task.id)
    assert t.status == "failed"
    assert "boom" in (t.error or "")


def test_approve_conflict_records_reason(repo, tmp_path):
    """A merge conflict at approve marks the task failed WITH the reason."""
    import asyncio
    import subprocess
    from pathlib import Path

    from sqlmodel import SQLModel, create_engine

    class EditAdapter(FakeAdapter):
        async def run(self, spec, ctx):
            yield AgentEvent(EventType.meta, data={"session_id": "x"})
            (Path(ctx.worktree_path) / "README.md").write_text("agent version\n")
            yield AgentEvent(EventType.final, text="done", data={"session_id": "x"})

    db = tmp_path / "conflict.db"
    eng = create_engine(f"sqlite:///{db}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(eng)
    orch = Orchestrator({"claude": EditAdapter()}, engine=eng)
    proj = orch.create_project("p", str(repo))
    task = orch.create_task(proj.id, "edit readme")
    asyncio.run(orch.run_task(task.id))

    # a conflicting edit lands on main before approval
    (repo / "README.md").write_text("main version\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "main edit"],
        cwd=repo, check=True, capture_output=True,
    )

    res = orch.approve_task(task.id)
    assert res.ok is False and res.conflict is True
    t = orch.get_task(task.id)
    assert t.status == "failed"
    assert "merge conflict" in (t.error or "")


def test_enabled_skill_injected_into_system_prompt(repo, tmp_path, monkeypatch):
    """An enabled skill's instructions reach the agent via the system prompt."""
    import asyncio

    from sqlmodel import SQLModel, create_engine

    from app import skills

    # a global skill store, monkeypatched away from ~/.conductor
    store = tmp_path / "skills"
    (store / "pdf").mkdir(parents=True)
    (store / "pdf" / "SKILL.md").write_text(
        "---\nname: pdf\ndescription: handle PDFs\n---\nUse pdfplumber to extract text.\n"
    )
    monkeypatch.setattr(skills, "SKILLS_DIR", store)
    assert {s["name"] for s in skills.list_skills()} == {"pdf"}

    db = tmp_path / "skills.db"
    eng = create_engine(f"sqlite:///{db}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(eng)
    fake = FakeAdapter()
    orch = Orchestrator({"claude": fake}, engine=eng)
    proj = orch.create_project("p", str(repo))
    orch.update_project_skills(proj.id, ["pdf"])
    task = orch.create_task(proj.id, "do a thing")

    asyncio.run(orch.run_task(task.id))

    assert fake.seen_system_prompts, "the adapter should have run"
    prompt = fake.seen_system_prompts[-1]
    assert "Skill: pdf" in prompt
    assert "pdfplumber" in prompt


def test_create_task_auto_agent_resolves(client, repo):
    """'auto' is accepted at the API and resolved to a real adapter (not 400)."""
    pid = client.post("/projects", json={"name": "p", "path": str(repo)}).json()["id"]
    r = client.post(f"/projects/{pid}/tasks", json={"title": "do a thing", "agent": "auto"})
    assert r.status_code == 201
    assert r.json()["assigned_agent"] != "auto"  # resolved to a registered adapter


def test_upload_task_attachments_are_injected(overridden, client, repo):
    _pid, tid = _make_project_and_task(client, repo)

    r = client.post(
        f"/tasks/{tid}/attachments",
        files=[
            ("files", ("shot.png", b"\x89PNG\r\n", "image/png")),
            ("files", ("../../notes.txt", b"look here", "text/plain")),
        ],
    )
    assert r.status_code == 200
    uploaded = r.json()
    assert [item["filename"] for item in uploaded] == ["shot.png", "notes.txt"]
    assert (repo / ".conductor" / "attachments" / f"task-{tid}" / "shot.png").exists()
    assert (repo / ".conductor" / "attachments" / f"task-{tid}" / "notes.txt").exists()

    async def flow():
        async with _asgi_client() as ac:
            run = await ac.post(f"/tasks/{tid}/run")
            assert run.status_code == 202
            await _drain_stream(ac, tid)

    asyncio.run(asyncio.wait_for(flow(), timeout=15))

    goal = overridden.adapters["claude"].seen_goals[-1]
    assert "Attachments supplied by the user:" in goal
    assert "shot.png" in goal
    assert "notes.txt" in goal
    diff = client.get(f"/tasks/{tid}/diff").json()["diff"]
    assert "hello from fake adapter" in diff
    assert "shot.png" not in diff
    assert "notes.txt" not in diff


def test_merge_records_handoff_memory(repo, tmp_path):
    """A merged task distills a handoff entry that later runs read back."""
    import asyncio
    from pathlib import Path

    from sqlmodel import SQLModel, create_engine

    from app import memory

    class EditAdapter(FakeAdapter):
        async def run(self, spec, ctx):
            yield AgentEvent(EventType.meta, data={"session_id": "x"})
            (Path(ctx.worktree_path) / "feature.txt").write_text("hello\n")
            yield AgentEvent(
                EventType.final, text="created feature.txt with a greeting",
                data={"session_id": "x"},
            )

    db = tmp_path / "handoff.db"
    eng = create_engine(f"sqlite:///{db}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(eng)
    orch = Orchestrator({"claude": EditAdapter()}, engine=eng)
    proj = orch.create_project("p", str(repo))
    task = orch.create_task(proj.id, "add a feature")
    asyncio.run(orch.run_task(task.id))

    assert orch.approve_task(task.id).ok is True

    # the merged task's distilled entry is now readable by future runs
    bundle = memory.build_context_bundle(proj.path)
    assert "add a feature" in bundle
    assert "feature.txt" in bundle
    assert "created feature.txt with a greeting" in bundle  # agent's exec summary


def test_reject_records_handoff_memory(repo, tmp_path):
    """A rejected task is recorded as a lesson in shared memory."""
    import asyncio
    from pathlib import Path

    from sqlmodel import SQLModel, create_engine

    from app import memory

    class EditAdapter(FakeAdapter):
        async def run(self, spec, ctx):
            yield AgentEvent(EventType.meta, data={"session_id": "x"})
            (Path(ctx.worktree_path) / "feature.txt").write_text("hello\n")
            yield AgentEvent(EventType.final, text="done", data={"session_id": "x"})

    db = tmp_path / "reject.db"
    eng = create_engine(f"sqlite:///{db}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(eng)
    orch = Orchestrator({"claude": EditAdapter()}, engine=eng)
    proj = orch.create_project("p", str(repo))
    task = orch.create_task(proj.id, "risky change")
    asyncio.run(orch.run_task(task.id))
    orch.reject_task(task.id)

    bundle = memory.build_context_bundle(proj.path)
    assert "risky change" in bundle
    assert "rejected" in bundle.lower()


def test_handoff_retrieved_by_keyword(repo):
    """build_context_bundle injects the handoffs relevant to the task query."""
    from app import memory

    mem = memory.memory_dir(repo)
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "handoff.md").write_text(
        "# Handoff\n\n"
        "### task 1: fix CSS formatting on the homepage\n- files: style.css\n\n"
        "### task 2: refactor the authentication and login flow\n- files: auth.py\n"
    )

    # a query about auth surfaces the auth entry and drops the unrelated one
    bundle = memory.build_context_bundle(repo, query="login auth flow problem")
    assert "authentication and login flow" in bundle
    assert "CSS formatting" not in bundle

    # no query -> recency fallback returns both
    both = memory.build_context_bundle(repo, query="")
    assert "task 1" in both and "task 2" in both


def test_distill_writes_insights_separate_from_global(repo, tmp_path):
    """distill_insights summarizes handoffs into insights.md (NOT global.md),
    and the distilled text is injected into future runs."""
    import asyncio

    from sqlmodel import SQLModel, create_engine

    from app import memory

    class SummarizeAdapter(FakeAdapter):
        async def run(self, spec, ctx):
            yield AgentEvent(EventType.meta, data={"session_id": "x"})
            yield AgentEvent(
                EventType.final,
                text="- prefer worktree isolation\n- mind the 64KB line buffer",
                data={"session_id": "x"},
            )

    db = tmp_path / "distill.db"
    eng = create_engine(f"sqlite:///{db}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(eng)
    orch = Orchestrator({"claude": SummarizeAdapter()}, engine=eng)
    proj = orch.create_project("p", str(repo))
    memory.record_handoff(proj.path, "### task 1: add worktrees\n- files: engine.py")

    out = asyncio.run(orch.distill_insights(proj.id))
    assert "prefer worktree isolation" in out

    mem = memory.memory_dir(proj.path)
    assert "prefer worktree isolation" in (mem / "insights.md").read_text()
    assert "prefer worktree isolation" not in (mem / "global.md").read_text()  # untouched
    assert "prefer worktree isolation" in memory.build_context_bundle(proj.path)  # injected


def test_distill_no_handoffs_is_noop(repo, tmp_path):
    """With no handoffs yet, distillation returns '' and writes nothing."""
    import asyncio

    from sqlmodel import SQLModel, create_engine

    from app import memory

    db = tmp_path / "distill_empty.db"
    eng = create_engine(f"sqlite:///{db}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(eng)
    orch = Orchestrator({"claude": FakeAdapter()}, engine=eng)
    proj = orch.create_project("p", str(repo))

    assert asyncio.run(orch.distill_insights(proj.id)) == ""
    assert not (memory.memory_dir(proj.path) / "insights.md").exists()


def test_auto_heal_revises_until_review_approves(repo, tmp_path):
    """With auto_heal_rounds set, a request_changes verdict auto-revises, and a
    clean re-review stops the loop — all before the human gate (never merges)."""
    verdicts = iter(["request_changes", "approve"])

    class HealAdapter(FakeAdapter):
        async def review(self, goal, diff, repo_path) -> ReviewResult:
            try:
                v = next(verdicts)
            except StopIteration:
                v = "approve"
            return ReviewResult(verdict=v, summary="self-review", findings=[])

    db = tmp_path / "heal.db"
    eng = create_engine(f"sqlite:///{db}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(eng)
    orch = Orchestrator({"claude": HealAdapter()}, engine=eng)
    proj = orch.create_project("p", str(repo))
    orch.update_project_auto_heal(proj.id, 2)
    task = orch.create_task(proj.id, "add feature")

    asyncio.run(orch.run_task(task.id))

    t = orch.get_task(task.id)
    assert t.status == "awaiting_approval"  # human still gates; never auto-merged
    # original run + exactly one auto-revise (loop stopped when the 2nd review approved)
    assert len(orch.list_runs(task.id)) == 2
    latest = orch.get_latest_review(task.id)
    assert latest is not None and latest.verdict == "approve"


def test_auto_heal_off_by_default(repo, tmp_path):
    """Default project has auto_heal_rounds=0: run_task does no review/revise."""
    db = tmp_path / "noheal.db"
    eng = create_engine(f"sqlite:///{db}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(eng)
    orch = Orchestrator({"claude": FakeAdapter()}, engine=eng)
    proj = orch.create_project("p", str(repo))
    assert proj.auto_heal_rounds == 0
    task = orch.create_task(proj.id, "add feature")

    asyncio.run(orch.run_task(task.id))

    t = orch.get_task(task.id)
    assert t.status == "awaiting_approval"
    assert len(orch.list_runs(task.id)) == 1  # no auto-revise run
    assert orch.get_latest_review(task.id) is None  # no auto-review


def test_handoff_cache_invalidates_after_record(repo):
    from app import memory

    memory.ensure_conductor(repo)
    memory.record_handoff(repo, "### task 1: auth flow\n- tags: #auth")
    first = memory.build_context_bundle(repo, query="auth", query_tags=["#auth"])
    assert "task 1" in first

    memory.record_handoff(repo, "### task 2: billing api\n- tags: #api")
    second = memory.build_context_bundle(repo, query="billing", query_tags=["#api"])
    assert "task 2" in second


def test_start_distill_insights_runs_in_background(repo, tmp_path):
    import asyncio

    from sqlmodel import SQLModel, create_engine

    from app import memory

    class SlowSummarizeAdapter(FakeAdapter):
        async def run(self, spec, ctx):
            await asyncio.sleep(0.01)
            yield AgentEvent(
                EventType.final,
                text="- keep memory distillation off the request path",
            )

    async def scenario():
        db = tmp_path / "distill_background.db"
        eng = create_engine(f"sqlite:///{db}", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(eng)
        orch = Orchestrator({"claude": SlowSummarizeAdapter()}, engine=eng)
        proj = orch.create_project("p", str(repo))
        memory.record_handoff(proj.path, "### task 1: async distill\n- tags: #perf")

        result = orch.start_distill_insights(proj.id)
        assert result == {"status": "distilling", "running": True}
        assert orch.start_distill_insights(proj.id) == {"status": "distilling", "running": True}

        for _ in range(20):
            insights = memory.memory_dir(proj.path) / "insights.md"
            if insights.exists():
                break
            await asyncio.sleep(0.01)

        assert "off the request path" in insights.read_text()

    asyncio.run(scenario())

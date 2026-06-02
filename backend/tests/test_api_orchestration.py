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

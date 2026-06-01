"""Codex CLI adapter.

Drives the official `codex` CLI in headless mode (`codex exec --json`) and
normalizes its thread/turn/item JSONL onto the shared AgentEvent contract.
Reuses the user's existing codex login — this adapter never handles credentials.

Calibrated against `codex-cli 0.128.0`:
  lifecycle (verified against live output):
    {"type":"thread.started","thread_id":"<uuid>"}        -> session id
    {"type":"turn.started"}
    {"type":"turn.completed","usage":{input_tokens,cached_input_tokens,output_tokens}}
    {"type":"turn.failed","error":{"message":...}}
    {"type":"error","message":...}                         -> stream-level failure
  items (from the official exec --json schema):
    {"type":"item.completed","item":{"type":"agent_message","text":...}}
    {"type":"item.completed","item":{"type":"reasoning","text":...}}
    {"type":"item.completed","item":{"type":"command_execution",
        "command","aggregated_output","exit_code","status"}}
    {"type":"item.completed","item":{"type":"file_change","changes":[{path,kind}]}}
    {"type":"item.completed","item":{"type":"mcp_tool_call","server","tool",...}}
    {"type":"item.completed","item":{"type":"web_search","query"}}
    {"type":"item.completed","item":{"type":"todo_list","items":[{text,completed}]}}
    {"type":"item.completed","item":{"type":"error","message"}}

Notes:
  - No `--append-system-prompt` equivalent exists; shared memory is folded into
    the prompt as a leading block (keeps the worktree clean -> no diff pollution).
  - Codex has no single "result" line; the last agent_message is treated as the
    final answer and surfaced on turn.completed.
  - A usage-limit ("You've hit your usage limit") is classified `quota`: the CLI
    is authenticated but cannot run, distinct from an `auth` failure.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from typing import AsyncIterator, Optional

from .base import (
    AgentAdapter,
    AgentEvent,
    EventType,
    HealthStatus,
    RunContext,
    TaskSpec,
)

CODEX_BIN = "codex"


class CodexAdapter(AgentAdapter):
    name = "codex"
    capabilities = {"code", "review", "test", "explain"}

    def __init__(self, bin_path: str = CODEX_BIN, sandbox: str = "workspace-write",
                 model: Optional[str] = None):
        self.bin = bin_path
        self.sandbox = sandbox
        self.model = model
        # Use model name as adapter name if specified for better identification
        if model:
            self.name = model
        self._last_session_id: Optional[str] = None
        self._last_message: str = ""

    def supports_resume(self) -> bool:
        return True

    # ----- command construction -----------------------------------------
    def _compose_prompt(self, spec: TaskSpec, ctx: RunContext) -> str:
        parts: list[str] = []
        if ctx.system_prompt:
            parts.append(ctx.system_prompt.strip())
            parts.append("\n---\n")
        parts.append(spec.goal)
        if spec.constraints:
            parts.append(f"\nConstraints:\n{spec.constraints}")
        if spec.acceptance:
            parts.append(f"\nAcceptance criteria:\n{spec.acceptance}")
        return "\n".join(parts)

    def _build_cmd(self, prompt: str, ctx: RunContext) -> list[str]:
        cmd = [self.bin, "exec"]
        resume = ctx.resume_session_id
        if resume:
            # `exec resume` restores the prior session's cwd/sandbox, so -C/-s
            # are neither accepted nor needed here.
            cmd += ["resume", resume]
        cmd += ["--json", "--skip-git-repo-check"]
        if not resume:
            cmd += ["-C", ctx.worktree_path, "-s", self.sandbox]
        # Fully autonomous within the sandbox: never block on an approval prompt
        # we can't answer in headless mode.
        cmd += ["-c", 'approval_policy="never"']
        if self.model:
            cmd += ["-m", self.model]
        cmd += [prompt]
        return cmd

    # ----- execution -----------------------------------------------------
    async def run(self, spec: TaskSpec, ctx: RunContext) -> AsyncIterator[AgentEvent]:
        self._last_session_id = None
        self._last_message = ""
        cmd = self._build_cmd(self._compose_prompt(spec, ctx), ctx)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=ctx.worktree_path,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        saw_error = False
        try:
            assert proc.stdout is not None
            async for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for ev in self._map_event(obj):
                    if ev.type == EventType.error:
                        saw_error = True
                    yield ev
            await proc.wait()
            if proc.returncode not in (0, None) and not saw_error:
                stderr = b""
                if proc.stderr is not None:
                    stderr = await proc.stderr.read()
                detail = stderr.decode("utf-8", "replace").strip()
                yield AgentEvent(
                    EventType.error,
                    text=detail or f"codex exited with code {proc.returncode}",
                    data={"kind": self._error_kind(detail), "returncode": proc.returncode},
                )
        finally:
            if proc.returncode is None:
                proc.kill()

    # ----- event normalization ------------------------------------------
    def _map_event(self, obj: dict) -> list[AgentEvent]:
        t = obj.get("type")
        if t == "thread.started":
            self._last_session_id = obj.get("thread_id")
            return [AgentEvent(EventType.meta, data={"session_id": obj.get("thread_id")})]
        if t == "turn.completed":
            return self._map_turn_completed(obj)
        if t == "turn.failed":
            msg = (obj.get("error") or {}).get("message", "")
            return [AgentEvent(EventType.error, text=msg,
                               data={"kind": self._error_kind(msg)})]
        if t == "error":
            msg = obj.get("message", "")
            return [AgentEvent(EventType.error, text=msg,
                               data={"kind": self._error_kind(msg)})]
        if t == "item.completed":
            return self._map_item(obj.get("item", {}) or {})
        # turn.started, item.started, item.updated: no normalized counterpart (yet)
        return []

    def _map_turn_completed(self, obj: dict) -> list[AgentEvent]:
        usage = obj.get("usage", {}) or {}
        return [
            AgentEvent(EventType.final, text=self._last_message,
                       data={"session_id": self._last_session_id}),
            AgentEvent(EventType.cost, data={
                "cost_usd": 0.0,  # subscription auth: codex reports tokens, not USD
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "duration_ms": 0,
            }),
        ]

    def _map_item(self, item: dict) -> list[AgentEvent]:
        it = item.get("type")
        if it == "agent_message":
            text = item.get("text", "")
            if text:
                self._last_message = text
            return [AgentEvent(EventType.message, text=text)]
        if it == "reasoning":
            return [AgentEvent(EventType.thinking, text=item.get("text", ""))]
        if it == "command_execution":
            cmd = item.get("command", "")
            exit_code = item.get("exit_code")
            out = [AgentEvent(EventType.tool_use, text=cmd, data={
                "name": "command", "command": cmd,
                "status": item.get("status"), "exit_code": exit_code,
            })]
            out.append(AgentEvent(EventType.tool_result,
                                  text=item.get("aggregated_output", "") or "",
                                  data={"exit_code": exit_code, "is_error": bool(exit_code)}))
            return out
        if it == "file_change":
            changes = item.get("changes", []) or []
            paths = [c.get("path", "") for c in changes]
            summary = ", ".join(p for p in paths if p)
            return [AgentEvent(EventType.tool_use, text=f"edit: {summary}", data={
                "name": "file_change", "changes": changes, "status": item.get("status"),
            })]
        if it == "mcp_tool_call":
            server, tool = item.get("server", ""), item.get("tool", "")
            out = [AgentEvent(EventType.tool_use, text=f"{server}.{tool}", data={
                "name": "mcp", "server": server, "tool": tool,
                "arguments": item.get("arguments"), "status": item.get("status"),
            })]
            if item.get("result") is not None or item.get("error"):
                out.append(AgentEvent(
                    EventType.tool_result,
                    text=self._mcp_text(item.get("result")),
                    data={"is_error": bool(item.get("error"))},
                ))
            return out
        if it == "web_search":
            q = item.get("query", "")
            return [AgentEvent(EventType.tool_use, text=f"web_search: {q}",
                               data={"name": "web_search", "query": q})]
        if it == "todo_list":
            return [AgentEvent(EventType.tool_use, text="todo_list",
                               data={"name": "todo_list", "items": item.get("items", [])})]
        if it == "error":
            msg = item.get("message", "")
            return [AgentEvent(EventType.error, text=msg,
                               data={"kind": self._error_kind(msg)})]
        # Unknown item type: surface it rather than silently dropping content.
        return [AgentEvent(EventType.tool_use, text=str(it or "item"),
                           data={"name": it, "raw": item})]

    @staticmethod
    def _error_kind(text: str) -> str:
        low = (text or "").lower()
        if any(k in low for k in ("usage limit", "rate limit", "quota", "credit")):
            return "quota"
        if any(k in low for k in ("authenticat", "unauthor", "401", "credential",
                                  "not logged", "please login", "please log in")):
            return "auth"
        return "runtime"

    @staticmethod
    def _mcp_text(result) -> str:
        if not isinstance(result, dict):
            return "" if result is None else str(result)
        blocks = result.get("content") or []
        if isinstance(blocks, list):
            return "".join(
                b.get("text", "") if isinstance(b, dict) else str(b) for b in blocks
            )
        return str(blocks)

    # ----- health --------------------------------------------------------
    async def healthcheck(self) -> HealthStatus:
        try:
            vproc = await asyncio.create_subprocess_exec(
                self.bin, "--version",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            vout, _ = await vproc.communicate()
        except FileNotFoundError:
            return HealthStatus(ok=False, auth_ok=False, detail=f"{self.bin} CLI not found")
        version = vout.decode("utf-8", "replace").strip().splitlines()[0] if vout else ""

        auth_ok = True
        rate_limited = False
        detail = "ok"
        with tempfile.TemporaryDirectory() as tmp:
            spec = TaskSpec(goal="Reply with exactly one word: pong")
            ctx = RunContext(worktree_path=tmp, timeout=60)
            try:
                async for ev in self.run(spec, ctx):
                    if ev.type == EventType.error:
                        kind = ev.data.get("kind")
                        if kind == "auth":
                            auth_ok = False
                            detail = ev.text or "authentication failed"
                        elif kind == "quota":
                            # Authenticated, just out of quota — surface but don't
                            # call it an auth failure.
                            rate_limited = True
                            detail = ev.text or "usage limit reached"
            except Exception as exc:  # noqa: BLE001
                return HealthStatus(ok=False, auth_ok=False, version=version, detail=str(exc))
        return HealthStatus(
            ok=True, auth_ok=auth_ok, rate_limited=rate_limited, version=version, detail=detail
        )

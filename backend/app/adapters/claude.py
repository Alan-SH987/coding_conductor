"""Claude Code adapter.

Drives the official `claude` CLI in headless mode and normalizes its
stream-json (NDJSON) output. Reuses the user's subscription login — this
adapter never handles credentials.

Calibrated against `claude 2.1.31` stream-json:
  - {"type":"system","subtype":"init", session_id, model, permissionMode, ...}
  - {"type":"assistant","message":{content:[{type:text|thinking|tool_use,...}]}, error?}
  - {"type":"user","message":{content:[{type:tool_result,...}]}}
  - {"type":"result","is_error":bool,"result":str,"total_cost_usd":...,"usage":{...}}
Note: auth failure surfaces as result.is_error=true (subtype still "success")
and an assistant-level error="authentication_failed".
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
    ReviewFinding,
    ReviewResult,
    RunContext,
    SubtaskSpec,
    TaskSpec,
)

CLAUDE_BIN = "claude"


def _extract_json_array(text: str) -> list:
    """Best-effort: pull the first top-level JSON array out of model output."""
    text = text.strip()
    if text.startswith("```"):  # strip a markdown fence + optional language hint
        text = text.strip("`")
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1:]
    try:
        v = json.loads(text)
        return v if isinstance(v, list) else []
    except json.JSONDecodeError:
        pass
    start = text.find("[")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            c = text[i]
            if c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    try:
                        v = json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        v = None
                    if isinstance(v, list):
                        return v
                    break  # this [...] wasn't a JSON array; try the next one
        start = text.find("[", start + 1)
    return []


def _extract_json_object(text: str) -> dict:
    """Best-effort: pull the first top-level JSON object out of model output."""
    text = text.strip()
    if text.startswith("```"):  # strip a markdown fence + optional language hint
        text = text.strip("`")
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1:]
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else {}
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            c = text[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        v = json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        v = None
                    if isinstance(v, dict):
                        return v
                    break  # this {...} wasn't a JSON object; try the next one
        start = text.find("{", start + 1)
    return {}


class ClaudeAdapter(AgentAdapter):
    name = "claude"
    capabilities = {"plan", "code", "review", "test", "explain"}

    def __init__(self, bin_path: str = CLAUDE_BIN):
        self.bin = bin_path
        self._last_session_id: Optional[str] = None

    def supports_resume(self) -> bool:
        return True

    # ----- command construction -----------------------------------------
    def _compose_prompt(self, spec: TaskSpec) -> str:
        parts = [spec.goal]
        if spec.constraints:
            parts.append(f"\nConstraints:\n{spec.constraints}")
        if spec.acceptance:
            parts.append(f"\nAcceptance criteria:\n{spec.acceptance}")
        return "\n".join(parts)

    def _build_cmd(self, prompt: str, ctx: RunContext) -> list[str]:
        cmd = [
            self.bin, "-p", prompt,
            "--output-format", "stream-json", "--verbose",
            "--permission-mode", ctx.permission_mode,
        ]
        if ctx.system_prompt:
            cmd += ["--append-system-prompt", ctx.system_prompt]
        if ctx.resume_session_id:
            cmd += ["--resume", ctx.resume_session_id]
        return cmd

    # ----- planning (read-only decomposition) ----------------------------
    PLAN_TIMEOUT = 180

    async def plan(
        self, goal: str, repo_path: str, capabilities: list[str]
    ) -> list[SubtaskSpec]:
        """Break a goal into subtasks by reading the repo in plan mode.

        ``--permission-mode plan`` keeps the run read-only (no edits), so this
        is safe to point at the repo root without dirtying the working tree.
        """
        cmd = [
            self.bin, "-p", self._compose_plan_prompt(goal, capabilities),
            "--output-format", "json",
            "--permission-mode", "plan",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=repo_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.PLAN_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("claude planning timed out")
        if proc.returncode not in (0, None):
            detail = stderr.decode("utf-8", "replace").strip()
            raise RuntimeError(detail or f"claude plan exited {proc.returncode}")

        result_text = self._result_text(stdout.decode("utf-8", "replace"))
        allowed = set(capabilities)
        fallback_cap = "code" if "code" in allowed else (
            sorted(allowed)[0] if allowed else "code")
        out: list[SubtaskSpec] = []
        for item in _extract_json_array(result_text):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "")).strip()
            if not title:
                continue
            cap = str(item.get("capability", "")).strip()
            out.append(SubtaskSpec(
                title=title,
                description=str(item.get("description", "")).strip(),
                capability=cap if cap in allowed else fallback_cap,
            ))
        return out

    @staticmethod
    def _result_text(stdout: str) -> str:
        """Pull the assistant's final text out of ``--output-format json``.

        That mode prints a single ``{... "result": "<text>"}`` object; fall back
        to raw stdout if it isn't shaped as expected.
        """
        stdout = stdout.strip()
        try:
            obj = json.loads(stdout)
            if isinstance(obj, dict) and "result" in obj:
                return str(obj["result"])
        except json.JSONDecodeError:
            pass
        return stdout

    @staticmethod
    def _compose_plan_prompt(goal: str, capabilities: list[str]) -> str:
        caps = ", ".join(sorted(capabilities))
        return (
            "You are a senior tech lead. Break the GOAL below into 2 to 5 "
            "concrete, independently mergeable engineering subtasks for THIS "
            "repository. Read the codebase (read-only) so each subtask names the "
            "real files or areas involved.\n\n"
            f"GOAL:\n{goal}\n\n"
            f"For each subtask pick the single best capability from: {caps}.\n\n"
            "Respond with ONLY a JSON array — no prose, no markdown fences. Each "
            "element must be exactly:\n"
            '{"title": "<short imperative>", "description": "<what to change and '
            'where, 1-3 sentences>", "capability": "<one of the listed>"}'
        )

    # ----- review (read-only diff critique) ------------------------------
    REVIEW_TIMEOUT = 180
    _VERDICTS = {"approve", "request_changes"}
    _SEVERITIES = {"blocker", "warning", "nit"}

    async def review(
        self, goal: str, diff: str, repo_path: str
    ) -> ReviewResult:
        """Critique a captured diff in plan mode (read-only, no edits).

        Same safety guarantee as ``plan()``: ``--permission-mode plan`` lets the
        reviewer read the repo for context without touching the working tree.
        """
        cmd = [
            self.bin, "-p", self._compose_review_prompt(goal, diff),
            "--output-format", "json",
            "--permission-mode", "plan",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=repo_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.REVIEW_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("claude review timed out")
        if proc.returncode not in (0, None):
            detail = stderr.decode("utf-8", "replace").strip()
            raise RuntimeError(detail or f"claude review exited {proc.returncode}")

        obj = _extract_json_object(self._result_text(stdout.decode("utf-8", "replace")))
        findings: list[ReviewFinding] = []
        for item in obj.get("findings") or []:
            if not isinstance(item, dict):
                continue
            comment = str(item.get("comment", "")).strip()
            if not comment:
                continue
            sev = str(item.get("severity", "")).strip().lower()
            findings.append(ReviewFinding(
                severity=sev if sev in self._SEVERITIES else "warning",
                comment=comment,
                file=str(item.get("file", "")).strip(),
            ))
        verdict = str(obj.get("verdict", "")).strip().lower()
        if verdict not in self._VERDICTS:
            # Infer a safe verdict when the model omits/garbles it: any blocker
            # means changes are needed, otherwise treat it as an approval.
            verdict = ("request_changes"
                       if any(f.severity == "blocker" for f in findings)
                       else "approve")
        return ReviewResult(
            verdict=verdict,
            summary=str(obj.get("summary", "")).strip(),
            findings=findings,
        )

    @staticmethod
    def _compose_review_prompt(goal: str, diff: str) -> str:
        return (
            "You are a meticulous senior code reviewer. Review the unified DIFF "
            "below against its GOAL. You may read the repository (read-only) for "
            "context. Judge correctness, completeness vs the goal, obvious bugs, "
            "and security issues. Be concise and specific.\n\n"
            f"GOAL:\n{goal}\n\n"
            f"DIFF:\n{diff}\n\n"
            "Respond with ONLY a JSON object — no prose, no markdown fences:\n"
            '{"verdict": "approve" | "request_changes", '
            '"summary": "<1-3 sentence overall assessment>", '
            '"findings": [{"severity": "blocker" | "warning" | "nit", '
            '"file": "<path or empty>", "comment": "<specific issue>"}]}\n'
            'Use "request_changes" if any blocker exists; otherwise "approve". '
            "An empty findings array is fine when the change is clean."
        )

    # ----- execution -----------------------------------------------------
    async def run(self, spec: TaskSpec, ctx: RunContext) -> AsyncIterator[AgentEvent]:
        cmd = self._build_cmd(self._compose_prompt(spec), ctx)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=ctx.worktree_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
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
                    yield ev
            await proc.wait()
            if proc.returncode not in (0, None):
                stderr = b""
                if proc.stderr is not None:
                    stderr = await proc.stderr.read()
                yield AgentEvent(
                    EventType.error,
                    text=stderr.decode("utf-8", "replace").strip() or f"exit {proc.returncode}",
                    data={"kind": "runtime", "returncode": proc.returncode},
                )
        finally:
            if proc.returncode is None:
                proc.kill()

    # ----- event normalization ------------------------------------------
    def _map_event(self, obj: dict) -> list[AgentEvent]:
        t = obj.get("type")
        if t == "system" and obj.get("subtype") == "init":
            self._last_session_id = obj.get("session_id")
            return [AgentEvent(EventType.meta, data={
                "session_id": obj.get("session_id"),
                "model": obj.get("model"),
                "permission_mode": obj.get("permissionMode"),
                "api_key_source": obj.get("apiKeySource"),
            })]
        if t == "assistant":
            return self._map_assistant(obj)
        if t == "user":
            return self._map_user(obj)
        if t == "result":
            return self._map_result(obj)
        return []

    def _map_assistant(self, obj: dict) -> list[AgentEvent]:
        out: list[AgentEvent] = []
        msg = obj.get("message", {}) or {}
        if obj.get("error"):
            err = str(obj["error"])
            out.append(AgentEvent(
                EventType.error,
                text=self._text_of(msg) or err,
                data={"kind": "auth" if "auth" in err.lower() else "runtime", "error": err},
            ))
        for block in msg.get("content", []) or []:
            bt = block.get("type")
            if bt == "text" and block.get("text"):
                out.append(AgentEvent(EventType.message, text=block["text"]))
            elif bt == "thinking":
                out.append(AgentEvent(EventType.thinking, text=block.get("thinking", "")))
            elif bt == "tool_use":
                out.append(AgentEvent(
                    EventType.tool_use,
                    text=block.get("name", ""),
                    data={"id": block.get("id"), "name": block.get("name"),
                          "input": block.get("input", {})},
                ))
        return out

    def _map_user(self, obj: dict) -> list[AgentEvent]:
        out: list[AgentEvent] = []
        for block in (obj.get("message", {}) or {}).get("content", []) or []:
            if block.get("type") == "tool_result":
                out.append(AgentEvent(
                    EventType.tool_result,
                    text=self._stringify(block.get("content")),
                    data={"tool_use_id": block.get("tool_use_id"),
                          "is_error": block.get("is_error", False)},
                ))
        return out

    def _map_result(self, obj: dict) -> list[AgentEvent]:
        out: list[AgentEvent] = []
        usage = obj.get("usage", {}) or {}
        if obj.get("is_error", False):
            text = obj.get("result", "")
            out.append(AgentEvent(
                EventType.error,
                text=text,
                data={"kind": self._error_kind(text), "subtype": obj.get("subtype")},
            ))
        else:
            out.append(AgentEvent(
                EventType.final,
                text=obj.get("result", ""),
                data={"session_id": obj.get("session_id"), "num_turns": obj.get("num_turns")},
            ))
        out.append(AgentEvent(EventType.cost, data={
            "cost_usd": obj.get("total_cost_usd", 0.0),
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "duration_ms": obj.get("duration_ms", 0),
        }))
        return out

    @staticmethod
    def _error_kind(text: str) -> str:
        low = text.lower()
        if "authenticat" in low or "401" in low or "credential" in low:
            return "auth"
        return "runtime"

    @staticmethod
    def _text_of(msg: dict) -> str:
        return "".join(
            b.get("text", "") for b in msg.get("content", []) or [] if b.get("type") == "text"
        )

    @staticmethod
    def _stringify(content) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                b.get("text", "") if isinstance(b, dict) else str(b) for b in content
            )
        return "" if content is None else str(content)

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
        detail = "ok"
        with tempfile.TemporaryDirectory() as tmp:
            spec = TaskSpec(goal="Reply with exactly one word: pong")
            ctx = RunContext(worktree_path=tmp, timeout=60)
            try:
                async for ev in self.run(spec, ctx):
                    if ev.type == EventType.error and ev.data.get("kind") == "auth":
                        auth_ok = False
                        detail = ev.text or "authentication failed"
            except Exception as exc:  # noqa: BLE001
                return HealthStatus(ok=False, auth_ok=False, version=version, detail=str(exc))
        return HealthStatus(ok=True, auth_ok=auth_ok, version=version, detail=detail)

"""Offline tests for the Codex adapter.

No subprocess: constructed thread/turn/item JSONL objects are fed to
`_map_event` / `_map_item`, and command construction is checked directly. The
schema mirrors `codex exec --json` (codex-cli 0.128.0); the usage-limit case
uses the verbatim line captured from a real run.
"""

from __future__ import annotations

from app.adapters.base import EventType, RunContext, TaskSpec
from app.adapters.codex import CodexAdapter


def _adapter() -> CodexAdapter:
    return CodexAdapter()


def _types(events) -> list[EventType]:
    return [e.type for e in events]


# --------------------------------------------------------------------------
# lifecycle events
# --------------------------------------------------------------------------
def test_thread_started_maps_to_meta():
    a = _adapter()
    evs = a._map_event({"type": "thread.started", "thread_id": "abc-123"})
    assert _types(evs) == [EventType.meta]
    assert evs[0].data["session_id"] == "abc-123"
    assert a._last_session_id == "abc-123"


def test_turn_started_is_ignored():
    assert _adapter()._map_event({"type": "turn.started"}) == []


def test_turn_completed_emits_final_then_cost():
    a = _adapter()
    a._map_event({"type": "thread.started", "thread_id": "t1"})
    a._map_item({"type": "agent_message", "text": "All done."})
    evs = a._map_event({
        "type": "turn.completed",
        "usage": {"input_tokens": 24763, "cached_input_tokens": 24448, "output_tokens": 122},
    })
    assert _types(evs) == [EventType.final, EventType.cost]
    final, cost = evs
    assert final.text == "All done."           # last agent_message becomes the result
    assert final.data["session_id"] == "t1"
    assert cost.data["input_tokens"] == 24763
    assert cost.data["output_tokens"] == 122
    assert cost.data["cost_usd"] == 0.0        # subscription auth -> no USD


def test_turn_failed_maps_to_error():
    evs = _adapter()._map_event({
        "type": "turn.failed",
        "error": {"message": "model response stream ended unexpectedly"},
    })
    assert _types(evs) == [EventType.error]
    assert evs[0].data["kind"] == "runtime"


# --------------------------------------------------------------------------
# item events
# --------------------------------------------------------------------------
def test_agent_message_maps_to_message_and_tracks_last():
    a = _adapter()
    evs = a._map_item({"type": "agent_message", "text": "Hello"})
    assert _types(evs) == [EventType.message]
    assert evs[0].text == "Hello"
    assert a._last_message == "Hello"


def test_reasoning_maps_to_thinking():
    evs = _adapter()._map_item({"type": "reasoning", "text": "**Scanning docs**"})
    assert _types(evs) == [EventType.thinking]
    assert evs[0].text == "**Scanning docs**"


def test_command_execution_emits_tool_use_and_result_success():
    evs = _adapter()._map_item({
        "type": "command_execution", "command": "bash -lc ls",
        "aggregated_output": "docs\n", "exit_code": 0, "status": "completed",
    })
    assert _types(evs) == [EventType.tool_use, EventType.tool_result]
    assert evs[0].text == "bash -lc ls"
    assert evs[1].text == "docs\n"
    assert evs[1].data["is_error"] is False


def test_command_execution_nonzero_exit_is_error():
    evs = _adapter()._map_item({
        "type": "command_execution", "command": "false",
        "aggregated_output": "", "exit_code": 2, "status": "failed",
    })
    assert evs[1].data["is_error"] is True


def test_file_change_maps_to_tool_use_with_paths():
    evs = _adapter()._map_item({
        "type": "file_change",
        "changes": [{"path": "docs/exec.md", "kind": "update"},
                    {"path": "hello.txt", "kind": "add"}],
        "status": "completed",
    })
    assert _types(evs) == [EventType.tool_use]
    assert "docs/exec.md" in evs[0].text and "hello.txt" in evs[0].text
    assert evs[0].data["changes"][1]["kind"] == "add"


def test_mcp_tool_call_maps_to_tool_use_and_result():
    evs = _adapter()._map_item({
        "type": "mcp_tool_call", "server": "docs", "tool": "search",
        "arguments": {"q": "exec --json"},
        "result": {"content": [{"type": "text", "text": "Found 3"}]},
        "status": "completed",
    })
    assert _types(evs) == [EventType.tool_use, EventType.tool_result]
    assert evs[0].text == "docs.search"
    assert evs[1].text == "Found 3"
    assert evs[1].data["is_error"] is False


def test_web_search_maps_to_tool_use():
    evs = _adapter()._map_item({"type": "web_search", "query": "codex exec schema"})
    assert _types(evs) == [EventType.tool_use]
    assert "codex exec schema" in evs[0].text


def test_todo_list_maps_to_tool_use():
    evs = _adapter()._map_item({
        "type": "todo_list", "items": [{"text": "Scan docs", "completed": True}],
    })
    assert _types(evs) == [EventType.tool_use]
    assert evs[0].data["items"][0]["text"] == "Scan docs"


def test_item_level_error_maps_to_error():
    evs = _adapter()._map_item({"type": "error", "message": "command output truncated"})
    assert _types(evs) == [EventType.error]


def test_unknown_item_is_surfaced_not_dropped():
    evs = _adapter()._map_item({"type": "future_thing", "blob": 1})
    assert _types(evs) == [EventType.tool_use]
    assert evs[0].data["name"] == "future_thing"


# --------------------------------------------------------------------------
# error classification (incl. the real captured usage-limit line)
# --------------------------------------------------------------------------
def test_usage_limit_line_classified_quota():
    real = ("You've hit your usage limit. Upgrade to Pro "
            "(https://chatgpt.com/explore/pro), visit "
            "https://chatgpt.com/codex/settings/usage to purchase more credits "
            "or try again at 1:44 PM.")
    evs = _adapter()._map_event({"type": "error", "message": real})
    assert _types(evs) == [EventType.error]
    assert evs[0].data["kind"] == "quota"


def test_error_kind_classification():
    k = CodexAdapter._error_kind
    assert k("Please login to continue") == "auth"
    assert k("401 Unauthorized") == "auth"
    assert k("invalid credentials") == "auth"
    assert k("You've hit your usage limit") == "quota"
    assert k("rate limit exceeded") == "quota"
    assert k("disk full") == "runtime"


# --------------------------------------------------------------------------
# command construction
# --------------------------------------------------------------------------
def test_build_cmd_fresh_run():
    a = _adapter()
    ctx = RunContext(worktree_path="/wt")
    cmd = a._build_cmd("do the thing", ctx)
    assert cmd[:2] == ["codex", "exec"]
    assert "--json" in cmd and "--skip-git-repo-check" in cmd
    assert "-C" in cmd and cmd[cmd.index("-C") + 1] == "/wt"
    assert "-s" in cmd and cmd[cmd.index("-s") + 1] == "workspace-write"
    assert 'approval_policy="never"' in cmd
    assert "resume" not in cmd
    assert cmd[-1] == "do the thing"


def test_build_cmd_resume_omits_cwd_and_sandbox():
    a = _adapter()
    ctx = RunContext(worktree_path="/wt", resume_session_id="sess-9")
    cmd = a._build_cmd("more work", ctx)
    assert cmd[:4] == ["codex", "exec", "resume", "sess-9"]
    assert "--json" in cmd
    # resume restores the session's own cwd/sandbox/model; we must not pass these.
    assert "-C" not in cmd
    assert "-s" not in cmd
    assert "-m" not in cmd
    assert cmd[-1] == "more work"


def test_build_cmd_defaults_to_supported_model():
    # No explicit model -> pin a base model the ChatGPT login accepts (not the
    # CLI's own gpt-5.3-codex default, which it rejects).
    cmd = CodexAdapter()._build_cmd("do it", RunContext(worktree_path="/wt"))
    assert "-m" in cmd and cmd[cmd.index("-m") + 1] == "gpt-5.5"


def test_system_prompt_is_prepended_to_prompt():
    a = _adapter()
    ctx = RunContext(worktree_path="/wt", system_prompt="PROJECT MEMORY: be careful")
    prompt = a._compose_prompt(TaskSpec(goal="fix bug"), ctx)
    assert prompt.startswith("PROJECT MEMORY: be careful")
    assert "fix bug" in prompt


# --------------------------------------------------------------------------
# full realistic sequence
# --------------------------------------------------------------------------
def test_full_sequence_happy_path():
    a = _adapter()
    stream = [
        {"type": "thread.started", "thread_id": "019e80e7"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "i0", "type": "reasoning",
                                            "text": "planning"}},
        {"type": "item.completed", "item": {"id": "i1", "type": "command_execution",
                                            "command": "bash -lc 'echo hi > hello.txt'",
                                            "aggregated_output": "", "exit_code": 0,
                                            "status": "completed"}},
        {"type": "item.completed", "item": {"id": "i2", "type": "file_change",
                                            "changes": [{"path": "hello.txt", "kind": "add"}],
                                            "status": "completed"}},
        {"type": "item.completed", "item": {"id": "i3", "type": "agent_message",
                                            "text": "Created hello.txt."}},
        {"type": "turn.completed",
         "usage": {"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 20}},
    ]
    collected = []
    for obj in stream:
        collected.extend(a._map_event(obj))
    kinds = _types(collected)
    assert kinds == [
        EventType.meta,
        EventType.thinking,
        EventType.tool_use, EventType.tool_result,   # command_execution
        EventType.tool_use,                          # file_change
        EventType.message,                           # agent_message
        EventType.final, EventType.cost,             # turn.completed
    ]
    # session id flows from thread.started through to the synthesized final
    assert collected[0].data["session_id"] == "019e80e7"
    final = collected[-2]
    assert final.text == "Created hello.txt."
    assert final.data["session_id"] == "019e80e7"


def test_real_captured_failure_sequence():
    """The exact event sequence captured from a live quota-exhausted run."""
    a = _adapter()
    stream = [
        {"type": "thread.started", "thread_id": "019e80e7-9db2-73e0-a35a-4d3cf0b10e24"},
        {"type": "turn.started"},
        {"type": "error", "message": "You've hit your usage limit. Upgrade to Pro."},
        {"type": "turn.failed",
         "error": {"message": "You've hit your usage limit. Upgrade to Pro."}},
    ]
    collected = []
    for obj in stream:
        collected.extend(a._map_event(obj))
    assert _types(collected) == [EventType.meta, EventType.error, EventType.error]
    assert all(e.data.get("kind") == "quota"
               for e in collected if e.type == EventType.error)

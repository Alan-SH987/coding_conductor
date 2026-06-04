"""Event-normalization tests for ClaudeAdapter.

These run offline (no login needed): they feed real claude 2.1.31 stream-json
records (captured during calibration) plus constructed success records into the
pure mapping layer.
"""

from app.adapters import ClaudeAdapter, EventType, RunContext, TaskSpec
from app.adapters._review import extract_json_object as _extract_json_object

# --- real records captured from `claude 2.1.31` (auth-failure path) ----------
INIT = {
    "type": "system", "subtype": "init", "cwd": "/private/tmp",
    "session_id": "95beec9a-41fb-4294-9c3b-15fdc12437fe",
    "model": "claude-opus-4-5-20251101", "permissionMode": "acceptEdits",
    "apiKeySource": "none", "claude_code_version": "2.1.31",
}
ASSISTANT_AUTHFAIL = {
    "type": "assistant",
    "message": {"role": "assistant", "content": [
        {"type": "text", "text": "Failed to authenticate. API Error: 401 Invalid authentication credentials"}
    ]},
    "error": "authentication_failed",
    "session_id": "95beec9a-41fb-4294-9c3b-15fdc12437fe",
}
RESULT_AUTHFAIL = {
    "type": "result", "subtype": "success", "is_error": True, "duration_ms": 634,
    "num_turns": 1, "result": "Failed to authenticate. API Error: 401 Invalid authentication credentials",
    "total_cost_usd": 0, "usage": {"input_tokens": 0, "output_tokens": 0},
    "session_id": "95beec9a-41fb-4294-9c3b-15fdc12437fe",
}

# --- constructed success-path records ----------------------------------------
ASSISTANT_TEXT = {"type": "assistant", "message": {"content": [{"type": "text", "text": "pong"}]}}
ASSISTANT_TOOLUSE = {"type": "assistant", "message": {"content": [
    {"type": "tool_use", "id": "tu_1", "name": "Read", "input": {"file_path": "/x.py"}}
]}}
USER_TOOLRESULT = {"type": "user", "message": {"content": [
    {"type": "tool_result", "tool_use_id": "tu_1", "content": "file contents", "is_error": False}
]}}
RESULT_OK = {
    "type": "result", "subtype": "success", "is_error": False, "result": "pong",
    "total_cost_usd": 0.012, "num_turns": 1, "duration_ms": 1500,
    "usage": {"input_tokens": 10, "output_tokens": 2}, "session_id": "abc",
}


def test_init_emits_meta_and_caches_session():
    a = ClaudeAdapter()
    evs = a._map_event(INIT)
    assert len(evs) == 1 and evs[0].type == EventType.meta
    assert evs[0].data["session_id"] == INIT["session_id"]
    assert evs[0].data["api_key_source"] == "none"
    assert a._last_session_id == INIT["session_id"]


def test_assistant_text_to_message():
    evs = ClaudeAdapter()._map_event(ASSISTANT_TEXT)
    assert [e.type for e in evs] == [EventType.message]
    assert evs[0].text == "pong"


def test_assistant_tool_use():
    evs = ClaudeAdapter()._map_event(ASSISTANT_TOOLUSE)
    assert evs[0].type == EventType.tool_use
    assert evs[0].data["name"] == "Read"
    assert evs[0].data["input"]["file_path"] == "/x.py"


def test_user_tool_result():
    evs = ClaudeAdapter()._map_event(USER_TOOLRESULT)
    assert evs[0].type == EventType.tool_result
    assert evs[0].data["tool_use_id"] == "tu_1"
    assert evs[0].text == "file contents"


def test_result_success_emits_final_then_cost():
    evs = ClaudeAdapter()._map_event(RESULT_OK)
    assert [e.type for e in evs] == [EventType.final, EventType.cost]
    assert evs[0].text == "pong"
    assert evs[1].data["cost_usd"] == 0.012
    assert evs[1].data["output_tokens"] == 2


def test_result_auth_failure_detected():
    evs = ClaudeAdapter()._map_event(RESULT_AUTHFAIL)
    assert evs[0].type == EventType.error
    assert evs[0].data["kind"] == "auth"
    assert evs[1].type == EventType.cost  # cost still reported


def test_assistant_level_auth_error_detected():
    evs = ClaudeAdapter()._map_event(ASSISTANT_AUTHFAIL)
    assert any(e.type == EventType.error and e.data.get("kind") == "auth" for e in evs)


def test_build_cmd_includes_expected_flags():
    ctx = RunContext(worktree_path="/wt", system_prompt="SYS", resume_session_id="sid-9")
    cmd = ClaudeAdapter()._build_cmd("do it", ctx)
    assert "--output-format" in cmd and "stream-json" in cmd
    assert "--verbose" in cmd
    assert "--permission-mode" in cmd and "acceptEdits" in cmd
    assert "--append-system-prompt" in cmd and "SYS" in cmd
    assert "--resume" in cmd and "sid-9" in cmd


def test_capabilities_and_resume():
    a = ClaudeAdapter()
    assert a.supports_resume() is True
    assert {"plan", "code", "review"} <= a.capabilities


# --- review JSON-object extraction (mirrors the planner's array extractor) ----
def test_extract_object_plain():
    obj = _extract_json_object('{"verdict": "approve", "findings": []}')
    assert obj["verdict"] == "approve" and obj["findings"] == []


def test_extract_object_strips_fence():
    raw = '```json\n{"verdict": "request_changes", "summary": "nope"}\n```'
    assert _extract_json_object(raw)["verdict"] == "request_changes"


def test_extract_object_embedded_in_prose():
    raw = 'Here is the review:\n{"verdict": "approve", "findings": [{"severity": "nit"}]}\nThanks!'
    obj = _extract_json_object(raw)
    assert obj["verdict"] == "approve"
    assert obj["findings"][0]["severity"] == "nit"


def test_extract_object_garbage_returns_empty():
    assert _extract_json_object("no json here at all") == {}

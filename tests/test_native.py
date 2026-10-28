from kanbeat.native import EventStream, VERDICT, command


def test_codex_completion_and_session():
    events = EventStream("codex")
    events.push({"type": "thread.started", "thread_id": "thread-1"})
    events.push({"type": "item.completed", "item": {
        "type": "agent_message", "text": "Finished",
    }})
    events.push({"type": "turn.completed"})
    result = events.result(0, "")
    assert result.ok
    assert result.session_id == "thread-1"
    assert result.answer == "Finished"


def test_claude_result_and_verdict():
    events = EventStream("claude")
    events.push({"type": "system", "session_id": "abc"})
    events.push({"type": "result", "subtype": "success",
                 "result": "All good\nKANBEAT_VERDICT: approve"})
    outcome = events.result(0, "")
    assert outcome.ok
    assert VERDICT.search(outcome.answer).group(1) == "approve"


def test_error_is_not_completion():
    events = EventStream("codex")
    events.push({"type": "turn.failed", "error": "failure"})
    assert not events.result(1, "").ok


def test_command_is_argv_without_permission_override():
    for harness in ("codex", "claude", "opencode", "pi"):
        argv, _ = command(harness, "Finish safely", None)
        assert argv[0] == harness
        assert "--dangerously-skip-permissions" not in argv
        assert "--yolo" not in argv
        assert "--auto" not in argv

"""Structured native turn completion and session ID parsing."""

from kanbeat.native import EventStream, VERDICT, command


def test_codex_completion_and_session():
    events = EventStream("codex")
    events.push({"type": "thread.started", "thread_id": "thread-1"})
    events.push(
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": "Finished",
            },
        }
    )
    events.push({"type": "turn.completed"})
    result = events.result(0, "")
    assert result.ok
    assert result.session_id == "thread-1"
    assert result.answer == "Finished"


def test_claude_result_and_verdict():
    events = EventStream("claude")
    events.push({"type": "system", "session_id": "abc"})
    events.push(
        {
            "type": "result",
            "subtype": "success",
            "result": "All good\nKANBEAT_VERDICT: approve",
        }
    )
    outcome = events.result(0, "")
    assert outcome.ok
    assert VERDICT.search(outcome.answer).group(1) == "approve"


def test_pi_settled_completion_reads_session_header_and_last_message():
    events = EventStream("pi")
    events.push({"type": "session", "version": 3, "id": "pi-session"})
    events.push(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "stop",
                "content": [{"type": "text", "text": "KANBEAT_VERDICT: approve"}],
            },
        }
    )
    events.push({"type": "agent_end"})
    events.push({"type": "agent_settled"})
    result = events.result(0, "")
    assert result.ok
    assert result.session_id == "pi-session"
    assert VERDICT.search(result.answer)


def test_pi_aborted_message_is_failure_even_with_zero_exit():
    events = EventStream("pi")
    events.push({"type": "session", "id": "pi-session"})
    events.push(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "aborted",
                "content": [],
            },
        }
    )
    events.push({"type": "agent_settled", "aborted": True})
    assert not events.result(0, "").ok


def test_native_activity_uses_explicit_events_only():
    codex = EventStream("codex")
    assert codex.activity is None
    codex.push({"type": "turn.started"})
    assert codex.activity == "working"
    codex.push({"type": "item.started"})
    assert codex.activity == "working"
    codex.push({"type": "turn.completed"})
    assert codex.activity == "idle"

    codex_error = EventStream("codex")
    codex_error.push({"type": "turn.started"})
    codex_error.push({"type": "error", "message": "stream failed"})
    assert codex_error.activity == "error"

    claude = EventStream("claude")
    claude.push({"type": "system", "session_id": "abc"})
    assert claude.activity is None
    claude.push({"type": "assistant"})
    assert claude.activity == "working"
    claude.push({"type": "result", "subtype": "success", "result": "done"})
    assert claude.activity == "idle"

    failed = EventStream("claude")
    failed.push({"type": "result", "subtype": "error", "is_error": True})
    assert failed.activity == "error"

    opencode = EventStream("opencode")
    opencode.push({"type": "step_start"})
    assert opencode.activity == "working"
    opencode.push({"type": "permission.asked"})
    assert opencode.activity == "waiting"
    opencode.push({"type": "permission.replied"})
    assert opencode.activity == "working"
    opencode.push({"type": "step_finish", "part": {"reason": "tool-calls"}})
    assert opencode.activity == "working"
    opencode.push({"type": "step_start"})
    opencode.push({"type": "step_finish", "part": {"reason": "stop"}})
    assert opencode.activity == "idle"

    pi = EventStream("pi")
    pi.push({"type": "agent_start"})
    assert pi.activity == "working"
    pi.push({"type": "agent_end"})
    assert pi.activity == "idle"

    aborted = EventStream("pi")
    aborted.push(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "aborted",
                "content": [],
            },
        }
    )
    assert aborted.activity == "error"


def test_error_is_not_completion():
    events = EventStream("codex")
    events.push({"type": "turn.failed", "error": "failure"})
    assert not events.result(1, "").ok


def test_commands_do_not_override_user_permissions():
    for harness in ("codex", "claude", "opencode", "pi"):
        argv, _ = command(harness, "Finish safely", None)
        assert argv[0] == harness
        assert "--dangerously-skip-permissions" not in argv
        assert "--yolo" not in argv
        assert "--auto" not in argv


def test_missing_session_error_alternative_word_order():
    events = EventStream("codex")
    events.push({"type": "error", "message": "Could not find session s-123"})
    assert events.result(1, "Could not find session s-123").resume_missing


def test_pi_failure_survives_later_successful_assistant_message():
    events = EventStream("pi")
    events.push(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "error",
                "errorMessage": "Provider failed",
            },
        }
    )
    events.push(
        {
            "type": "turn_end",
            "message": {
                "role": "assistant",
                "stopReason": "stop",
                "content": [{"type": "text", "text": "Finished"}],
            },
        }
    )
    events.push({"type": "agent_settled"})
    outcome = events.result(0, "")
    assert not outcome.ok
    assert outcome.reason == "Provider failed"

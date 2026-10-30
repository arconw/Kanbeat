import io
import json
from pathlib import Path

from kanbeat.native_events import (
    bridge_command,
    main,
    normalize_hook,
    normalize_statusline,
    read_events,
    record_event,
    settings_json,
)


def test_normalizes_stop_without_transcript_or_prompt_fields():
    record = normalize_hook(
        {
            "hook_event_name": "Stop",
            "session_id": "session-1",
            "transcript_path": "/secret/transcript.jsonl",
            "last_assistant_message": "KANBEAT_VERDICT: approve",
            "stop_hook_active": False,
            "effort": {"level": "high"},
            "permission_mode": "auto",
        }
    )
    assert record["event"] == "Stop"
    assert record["activity"] == "done"
    assert record["message"] == "KANBEAT_VERDICT: approve"
    assert record["final"] is True
    assert record["effort"] == "high"
    assert record["permission_mode"] == "auto"
    assert "transcript" not in json.dumps(record)


def test_stop_message_keeps_the_tail_for_verdicts():
    message = "x" * 40000 + "\nKANBEAT_VERDICT: approve"
    record = normalize_hook(
        {
            "hook_event_name": "Stop",
            "session_id": "session-1",
            "last_assistant_message": message,
        }
    )
    assert record["message"].endswith("KANBEAT_VERDICT: approve")
    assert len(record["message"]) == 32000


def test_normalizes_failure_and_activity_events():
    failure = normalize_hook(
        {
            "hook_event_name": "StopFailure",
            "session_id": "session-1",
            "last_assistant_message": "Not logged in",
            "error": "authentication_failed",
        }
    )
    assert failure["activity"] == "error"
    assert failure["final"] is False
    assert failure["error"] == "authentication_failed"
    working = normalize_hook(
        {"hook_event_name": "UserPromptSubmit", "session_id": "session-1"}
    )
    assert working["activity"] == "working"
    tool = normalize_hook(
        {
            "hook_event_name": "PreToolUse",
            "session_id": "session-1",
            "tool_name": "Bash",
        }
    )
    assert tool["activity"] == "tool"
    assert tool["tool"] == "Bash"
    assert normalize_hook({"hook_event_name": "Unknown"}) is None


def test_statusline_records_only_model_effort_and_session():
    payload = {
        "session_id": "session-1",
        "model": {"id": "claude-x", "display_name": "Claude"},
        "effort": {"level": "high"},
        "cost": {"total_cost_usd": 3},
        "workspace": {"current_dir": "/secret"},
    }
    record = normalize_statusline(payload)
    assert record == {
        "event": "Statusline",
        "activity": "settings",
        "session_id": "session-1",
        "model": "claude-x",
        "effort": "high",
    }
    assert normalize_statusline({"session_id": "s"}) is None


def test_statusline_state_is_written_only_after_append(tmp_path: Path, monkeypatch):
    import kanbeat.native_events as module

    payload = {
        "session_id": "session-1",
        "model": {"id": "claude-x"},
        "effort": {"level": "high"},
    }
    monkeypatch.setattr(module, "append_record", lambda path, record: False)
    assert record_event("statusline", payload, tmp_path, "config") is False
    assert not (tmp_path / "config.settings.json").exists()


def test_statusline_deduplicates_and_events_survive_offsets(tmp_path: Path):
    payload = {
        "session_id": "session-1",
        "model": {"id": "claude-x"},
        "effort": {"level": "high"},
    }
    assert record_event("statusline", payload, tmp_path, "config") is True
    assert record_event("statusline", payload, tmp_path, "config") is False
    assert record_event(
        "hook",
        {
            "hook_event_name": "Stop",
            "session_id": "session-1",
            "last_assistant_message": "x",
        },
        tmp_path,
        "config",
    )
    records, offset = read_events(tmp_path / "config.jsonl")
    assert [record["event"] for record in records] == ["Statusline", "Stop"]
    assert read_events(tmp_path / "config.jsonl", offset)[0] == []


def test_bridge_commands_are_shell_quoted_and_private(tmp_path: Path):
    command = bridge_command(["python", "-m", "kanbeat"], tmp_path, "worker-1", "hook")
    assert str(tmp_path) in command
    assert "--terminal-id worker-1" in command
    settings = json.loads(settings_json(["python", "-m", "kanbeat"], tmp_path, "cfg"))
    assert set(settings["hooks"]) == {
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "Stop",
        "StopFailure",
        "Notification",
        "SessionEnd",
    }
    assert "statusLine" in settings


def test_main_is_silent_and_accepts_hook_payload(tmp_path: Path):
    payload = json.dumps(
        {
            "hook_event_name": "Stop",
            "session_id": "session-1",
            "last_assistant_message": "done",
        }
    ).encode()
    assert (
        main(
            ["--events-dir", str(tmp_path), "--terminal-id", "worker-1", "hook"],
            stdin=io.BytesIO(payload),
        )
        == 0
    )
    records, _ = read_events(tmp_path / "worker-1.jsonl")
    assert records[0]["message"] == "done"

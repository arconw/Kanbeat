import importlib
import json
import os
from pathlib import Path

import pytest

from kanbeat import pi_history
from kanbeat.session_transfer import render_short_prompt

SESSION = "review-fixture"
PROJECT = "--review-project--"


def header(session_id=SESSION):
    return {
        "type": "session",
        "version": 3,
        "id": session_id,
        "timestamp": "2030-01-01T00:00:00.000Z",
        "cwd": "/fixture/project",
    }


def entry(identifier, parent, entry_type, **fields):
    return {
        "type": entry_type,
        "id": identifier,
        "parentId": parent,
        "timestamp": "2030-01-01T00:00:01.000Z",
        **fields,
    }


def message(identifier, parent, message, **fields):
    return entry(identifier, parent, "message", message=message, **fields)


def write_session(tmp_path, records, session_id=SESSION, project=PROJECT):
    directory = tmp_path / "sessions" / project
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"2030-01-01T00-00-00-000Z_{session_id}.jsonl"
    path.write_text("".join(json.dumps(item) + "\n" for item in records))
    return tmp_path / "sessions", path


def test_review_reader_uses_configured_directory_and_explicit_override(
    tmp_path, monkeypatch
):
    records = [
        header(),
        message("user-one", None, {"role": "user", "content": "Fixture question"}),
    ]
    sessions, path = write_session(tmp_path, records)
    monkeypatch.setenv("PI_CODING_AGENT_SESSION_DIR", str(sessions))

    configured = pi_history.read_pi_history(SESSION)

    assert configured.available and configured.session_file == path
    assert [event.event_id for event in configured.events] == ["user-one"]

    override = pi_history.read_pi_history(SESSION, sessions_dir=tmp_path / "elsewhere")
    assert override.missing and not override.events


def test_review_import_is_lazy_and_does_not_access_history(tmp_path, monkeypatch):
    write_session(
        tmp_path,
        [
            header(),
            message("user-one", None, {"role": "user", "content": "Fixture"}),
        ],
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("history was accessed during module import")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", forbidden)
        patch.setattr(Path, "glob", forbidden)
        patch.setattr(os, "scandir", forbidden)
        importlib.reload(pi_history)

    assert pi_history.HARNESS == "pi"


def test_review_real_shaped_history_is_ordered_labelled_and_read_only(
    tmp_path, capsys, caplog
):
    marker = render_short_prompt(
        "Read the review context.", "review-import", ["codex"], 0, 4
    )
    records = [
        header(),
        message(
            "system-one",
            None,
            {"role": "system", "content": "", "sections": {"preamble": "Fixture"}},
        ),
        message(
            "user-one",
            "system-one",
            {"role": "user", "content": "Fixture question"},
        ),
        message(
            "assistant-one",
            "user-one",
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "PRIVATE_REVIEW_REASONING"},
                    {"type": "text", "text": "Fixture answer"},
                    {
                        "type": "toolCall",
                        "id": "call-one",
                        "name": "bash",
                        "arguments": {"command": "pwd"},
                    },
                ],
                "stopReason": "toolUse",
            },
        ),
        message(
            "tool-one",
            "assistant-one",
            {
                "role": "toolResult",
                "toolCallId": "call-one",
                "toolName": "bash",
                "content": [
                    {"type": "text", "text": "Fixture output"},
                    {"type": "image", "data": "PRIVATE_REVIEW_IMAGE"},
                ],
                "isError": False,
            },
        ),
        entry("usage-one", "tool-one", "usage", kind="cache_warm", usage={}),
        entry(
            "compact-one",
            "usage-one",
            "compaction",
            summary="PRIVATE_REVIEW_SUMMARY",
            firstKeptEntryId="user-one",
        ),
        message(
            "import-one",
            "compact-one",
            {"role": "user", "content": marker},
        ),
        message(
            "assistant-two",
            "import-one",
            {"role": "assistant", "content": [{"type": "text", "text": "Reply"}]},
        ),
    ]
    sessions, path = write_session(tmp_path, records)
    before = path.read_bytes(), path.stat().st_mtime_ns
    result = pi_history.read_pi_history(SESSION, sessions_dir=sessions, ordinal_start=8)
    again = pi_history.read_pi_history(SESSION, sessions_dir=sessions, ordinal_start=8)

    assert result.available and result.complete
    assert result.events == again.events
    assert [event.event_id for event in result.events] == [
        "user-one",
        "assistant-one",
        "assistant-one:1",
        "tool-one",
        "import-one",
        "assistant-two",
    ]
    assert [event.ordinal for event in result.events] == [8, 9, 10, 11, 12, 13]
    assert [event.position for event in result.events] == [2, 3, 3, 4, 7, 8]
    assert [event.role for event in result.events] == [
        "user",
        "assistant",
        "tool_call",
        "tool_result",
        "user",
        "assistant",
    ]
    assert result.events[1].text == "Fixture answer"
    assert result.events[2].label == "Tool call (bash)"
    assert result.events[3].label == "Tool result (bash)"
    assert result.events[3].text == "Fixture output"
    assert result.events[4].is_import
    assert result.events[4].import_id == "review-import"
    assert set(result.unsupported) == {
        "hidden reasoning",
        "images",
        "compaction summaries",
    }
    assert "PRIVATE_REVIEW" not in repr(result.events)
    assert caplog.text == ""
    assert capsys.readouterr() == ("", "")
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_review_missing_and_temporary_failures_are_distinct(tmp_path, monkeypatch):
    missing = pi_history.read_pi_history(SESSION, sessions_dir=tmp_path / "absent")
    assert missing.missing and not missing.unreachable
    assert not missing.complete and not missing.events
    assert missing.detail

    sessions, path = write_session(
        tmp_path,
        [
            header(),
            message("user-one", None, {"role": "user", "content": "Fixture"}),
        ],
    )
    original_open = Path.open

    def denied(self, *args, **kwargs):
        if self == path:
            raise PermissionError("fixture read denied")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    unreachable = pi_history.read_pi_history(SESSION, sessions_dir=sessions)
    assert unreachable.unreachable and not unreachable.missing
    assert not unreachable.complete and not unreachable.events
    assert unreachable.detail

    original_scandir = os.scandir

    def denied_scandir(directory):
        if Path(directory) == path.parent:
            raise PermissionError("fixture listing denied")
        return original_scandir(directory)

    monkeypatch.setattr(os, "scandir", denied_scandir)
    listing = pi_history.read_pi_history(SESSION, sessions_dir=sessions)
    assert listing.unreachable
    assert not listing.missing


def test_review_unreadable_unrelated_project_does_not_hide_history(
    tmp_path, monkeypatch
):
    sessions, path = write_session(
        tmp_path,
        [
            header(),
            message("user-one", None, {"role": "user", "content": "Fixture"}),
        ],
    )
    unrelated = sessions / "--unrelated--"
    unrelated.mkdir()
    original_scandir = os.scandir

    def denied(directory):
        if Path(directory) == unrelated:
            raise PermissionError("fixture unrelated denied")
        return original_scandir(directory)

    monkeypatch.setattr(os, "scandir", denied)
    result = pi_history.read_pi_history(SESSION, sessions_dir=sessions)
    assert result.available and result.complete
    assert result.session_file == path


def test_review_malformed_history_is_available_but_incomplete(tmp_path):
    records = [
        header(),
        message("user-one", None, {"role": "user", "content": "Fixture"}),
        message(
            "assistant-one",
            "user-one",
            {"role": "assistant", "content": [{"type": "text", "text": "Reply"}]},
        ),
    ]
    sessions, path = write_session(tmp_path, records)
    with path.open("ab") as handle:
        handle.write(b"{partial\n")
        handle.write(b"\xff\n")

    result = pi_history.read_pi_history(SESSION, sessions_dir=sessions)

    assert result.available and not result.complete
    assert [event.event_id for event in result.events] == [
        "user-one",
        "assistant-one",
    ]


def test_review_header_mismatch_and_absence_are_missing(tmp_path):
    sessions, _ = write_session(
        tmp_path,
        [
            header(session_id="another-session"),
            message("user-one", None, {"role": "user", "content": "Fixture"}),
        ],
    )
    mismatch = pi_history.read_pi_history(SESSION, sessions_dir=sessions)
    assert mismatch.missing and not mismatch.events

    sessions, _ = write_session(
        tmp_path,
        [message("user-one", None, {"role": "user", "content": "Fixture"})],
        session_id="no-header",
    )
    absent = pi_history.read_pi_history("no-header", sessions_dir=sessions)
    assert absent.missing and not absent.events


def test_review_suffix_match_with_another_header_id_is_not_used(tmp_path):
    longer = f"b_{SESSION}"
    sessions, target = write_session(
        tmp_path,
        [
            header(),
            message("target-one", None, {"role": "user", "content": "Target"}),
        ],
    )
    _, other = write_session(
        tmp_path,
        [
            header(session_id=longer),
            message("other-one", None, {"role": "user", "content": "Other"}),
        ],
        session_id=longer,
    )
    assert other.name.endswith(f"_{SESSION}.jsonl")

    result = pi_history.read_pi_history(SESSION, sessions_dir=sessions)

    assert result.available and result.complete
    assert result.session_file == target
    assert [event.event_id for event in result.events] == ["target-one"]

    target.unlink()
    absent = pi_history.read_pi_history(SESSION, sessions_dir=sessions)

    assert absent.missing and not absent.events


def test_review_file_change_during_read_is_incomplete(tmp_path, monkeypatch):
    records = [
        header(),
        message("user-one", None, {"role": "user", "content": "Fixture"}),
    ]
    sessions, path = write_session(tmp_path, records)
    original_stat = Path.stat
    calls = {"count": 0}

    class Snapshot:
        def __init__(self, result, mtime_ns):
            self.st_mode = result.st_mode
            self.st_size = result.st_size
            self.st_ino = result.st_ino
            self.st_mtime_ns = mtime_ns

    def stat(self, *args, **kwargs):
        result = original_stat(self, *args, **kwargs)
        if self == path:
            calls["count"] += 1
            if calls["count"] > 1:
                return Snapshot(result, result.st_mtime_ns + 1)
        return result

    monkeypatch.setattr(Path, "stat", stat)
    result = pi_history.read_pi_history(SESSION, sessions_dir=sessions)

    assert result.available and not result.complete
    assert [event.event_id for event in result.events] == ["user-one"]


def test_review_import_envelope_split_across_blocks_and_bare_marker(tmp_path):
    marker = render_short_prompt(
        "Read the review context.", "review-import", ["codex"], 0, 1
    )
    blocks = [{"type": "text", "text": line} for line in marker.splitlines()]
    records = [
        header(),
        message("split-import", None, {"role": "user", "content": blocks}),
        message(
            "bare-marker",
            "split-import",
            {"role": "user", "content": f"Quoted marker: {marker}"},
        ),
    ]
    sessions, _ = write_session(tmp_path, records)

    result = pi_history.read_pi_history(SESSION, sessions_dir=sessions)

    assert result.available and result.complete
    assert "\n".join(event.text for event in result.events[:3]) == marker
    assert all(event.is_import for event in result.events[:3])
    assert {event.import_id for event in result.events[:3]} == {"review-import"}
    assert not result.events[3].is_import
    assert result.events[3].import_id is None


def test_review_unsupported_categories_and_tool_labels(tmp_path):
    records = [
        header(),
        message("user-one", None, {"role": "user", "content": "Fixture"}),
        message(
            "bash-one",
            "user-one",
            {
                "role": "bashExecution",
                "command": "pwd",
                "output": "/fixture",
                "truncated": True,
                "excludeFromContext": False,
            },
        ),
        message(
            "custom-role",
            "bash-one",
            {"role": "custom", "content": "PRIVATE_REVIEW_CUSTOM"},
        ),
        entry("custom-message", "custom-role", "custom_message", content="Fixture"),
        entry("branch", "custom-message", "branch_summary", summary="PRIVATE"),
        entry("edit", "branch", "context_edit", targetId="user-one"),
        entry("future", "edit", "future_entry"),
    ]
    sessions, _ = write_session(tmp_path, records)

    result = pi_history.read_pi_history(SESSION, sessions_dir=sessions)

    assert result.available and result.complete
    assert [event.event_id for event in result.events] == ["user-one", "bash-one"]
    assert result.events[1].role == "tool_call"
    assert result.events[1].tool_name == "bash"
    assert result.events[1].text == "pwd\n\nOutput:\n/fixture"
    assert set(result.unsupported) == {
        "truncated bash output",
        "custom messages",
        "branch summaries",
        "context edits",
        "unknown content",
    }
    assert "PRIVATE_REVIEW_CUSTOM" not in repr(result.events)


def test_review_bash_excluded_from_context_is_not_exported(tmp_path):
    records = [
        header(),
        message("user-one", None, {"role": "user", "content": "Run the fixture"}),
        message(
            "bash-excluded",
            "user-one",
            {
                "role": "bashExecution",
                "command": "cat fixture",
                "output": "PRIVATE_REVIEW_EXCLUDED",
                "excludeFromContext": True,
            },
        ),
        message(
            "bash-visible",
            "bash-excluded",
            {
                "role": "bashExecution",
                "command": "pwd",
                "output": "/fixture",
                "excludeFromContext": False,
            },
        ),
    ]
    sessions, path = write_session(tmp_path, records)
    before = path.read_bytes(), path.stat().st_mtime_ns

    result = pi_history.read_pi_history(SESSION, sessions_dir=sessions)

    assert result.available and result.complete
    assert [event.event_id for event in result.events] == ["user-one", "bash-visible"]
    assert "PRIVATE_REVIEW_EXCLUDED" not in repr(result.events)
    assert result.unsupported == ("excluded bash output",)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_review_invalid_session_id_is_rejected(tmp_path):
    sessions, _ = write_session(tmp_path, [header()])

    for value in ("../fixture", "a/b", "", "x" * 129):
        with pytest.raises(ValueError):
            pi_history.read_pi_history(value, sessions_dir=sessions)

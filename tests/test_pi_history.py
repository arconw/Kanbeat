import json

import pytest

from kanbeat import pi_history
from kanbeat.session_transfer import render_short_prompt

SESSION = "fixture-session"


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


def session_file(tmp_path, records, session_id=SESSION):
    folder = tmp_path / "sessions" / "--fixture-project--"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"2030-01-01T00-00-00-000Z_{session_id}.jsonl"
    path.write_text("".join(json.dumps(item) + "\n" for item in records))
    return tmp_path / "sessions", path


def test_real_shaped_active_branch_ids_roles_tools_imports_and_read_only(
    tmp_path, caplog, capsys
):
    marker = render_short_prompt(
        "Read the fixture context.", "fixture-import", ["codex"], 0, 5
    )
    records = [
        header(),
        message(
            "system-one",
            None,
            {
                "role": "system",
                "content": "",
                "sections": {"preamble": "Fixture system prompt"},
                "timestamp": 1,
            },
        ),
        message(
            "user-one",
            "system-one",
            {"role": "user", "content": "Fixture question", "timestamp": 2},
        ),
        entry(
            "model-one",
            "user-one",
            "model_change",
            provider="fixture-provider",
            modelId="fixture-model",
        ),
        message(
            "assistant-one",
            "model-one",
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "PRIVATE_FIXTURE_REASONING"},
                    {"type": "text", "text": "Fixture answer"},
                    {
                        "type": "toolCall",
                        "id": "call-one",
                        "name": "bash",
                        "arguments": {"command": "pwd"},
                    },
                ],
                "stopReason": "toolUse",
                "timestamp": 3,
            },
        ),
        message(
            "tool-one",
            "assistant-one",
            {
                "role": "toolResult",
                "toolCallId": "call-one",
                "toolName": "bash",
                "content": [{"type": "text", "text": "Fixture result"}],
                "isError": False,
                "timestamp": 4,
            },
        ),
        entry(
            "usage-one",
            "tool-one",
            "usage",
            kind="cache_warm",
            usage={"totalTokens": 10},
        ),
        entry(
            "compact-one",
            "usage-one",
            "compaction",
            summary="PRIVATE_FIXTURE_SUMMARY",
            firstKeptEntryId="user-one",
            tokensBefore=10,
        ),
        message(
            "import-one",
            "compact-one",
            {"role": "user", "content": marker, "timestamp": 5},
        ),
        message(
            "assistant-two",
            "import-one",
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "New reply"}],
                "stopReason": "stop",
                "timestamp": 6,
            },
        ),
    ]
    sessions, path = session_file(tmp_path, records)
    before = path.read_bytes(), path.stat().st_mtime_ns

    assert not caplog.records
    result = pi_history.read_pi_history(SESSION, sessions_dir=sessions, ordinal_start=5)

    assert result.available and result.complete
    assert result.session_file == path
    assert [event.event_id for event in result.events] == [
        "user-one",
        "assistant-one",
        "assistant-one:1",
        "tool-one",
        "import-one",
        "assistant-two",
    ]
    assert [event.ordinal for event in result.events] == [5, 6, 7, 8, 9, 10]
    assert [event.position for event in result.events] == [2, 4, 4, 5, 8, 9]
    assert [event.role for event in result.events] == [
        "user",
        "assistant",
        "tool_call",
        "tool_result",
        "user",
        "assistant",
    ]
    assert result.events[1].text == "Fixture answer"
    assert result.events[2].render() == 'Tool call (bash): {"command":"pwd"}'
    assert result.events[3].render() == "Tool result (bash): Fixture result"
    assert [(event.is_import, event.import_id) for event in result.events] == [
        (False, None),
        (False, None),
        (False, None),
        (False, None),
        (True, "fixture-import"),
        (False, None),
    ]
    assert set(result.unsupported) == {"hidden reasoning", "compaction summaries"}
    assert "PRIVATE_FIXTURE" not in repr(result.events)
    assert "PRIVATE_FIXTURE" not in caplog.text
    assert "PRIVATE_FIXTURE" not in capsys.readouterr().out
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert result.history is not None and result.history.harness == "pi"


def test_last_appended_entry_defines_the_active_parent_chain(tmp_path):
    records = [
        header(),
        message("user-one", None, {"role": "user", "content": "Fixture question"}),
        message(
            "assistant-main",
            "user-one",
            {"role": "assistant", "content": [{"type": "text", "text": "Main"}]},
        ),
        message(
            "user-two",
            "assistant-main",
            {"role": "user", "content": "Fixture follow-up"},
        ),
        message(
            "assistant-branch",
            "user-one",
            {"role": "assistant", "content": [{"type": "text", "text": "Branch"}]},
        ),
    ]
    sessions, _ = session_file(tmp_path, records)

    result = pi_history.read_pi_history(SESSION, sessions_dir=sessions)

    assert result.available and result.complete
    assert [event.event_id for event in result.events] == [
        "user-one",
        "assistant-branch",
    ]


def test_missing_history_and_a_listing_failure_are_different(tmp_path, monkeypatch):
    missing = pi_history.read_pi_history(SESSION, sessions_dir=tmp_path / "absent")
    assert missing.missing and not missing.complete and not missing.events
    assert missing.detail

    def fail(_path):
        raise PermissionError("fixture listing failure")

    monkeypatch.setattr(pi_history.os, "scandir", fail)
    unreachable = pi_history.read_pi_history(SESSION, sessions_dir=tmp_path)
    assert unreachable.unreachable and not unreachable.complete
    assert not unreachable.events
    assert unreachable.detail


def test_malformed_lines_and_bare_markers_are_not_silently_accepted(tmp_path):
    marker = render_short_prompt(
        "Read the fixture context.", "fixture-import", ["codex"], 0, 1
    )
    records = [
        header(),
        message(
            "user-one",
            None,
            {"role": "user", "content": f"Quoted marker: {marker}"},
        ),
        message(
            "assistant-one",
            "user-one",
            {"role": "assistant", "content": [{"type": "text", "text": "Fixture"}]},
        ),
    ]
    sessions, path = session_file(tmp_path, records)
    with path.open("a") as handle:
        handle.write("{not-json\n")

    result = pi_history.read_pi_history(SESSION, sessions_dir=sessions)

    assert result.available and not result.complete
    assert [event.event_id for event in result.events] == ["user-one", "assistant-one"]
    assert not result.events[0].is_import


def test_empty_session_and_invalid_identifier(tmp_path):
    sessions, path = session_file(tmp_path, [header()])
    result = pi_history.read_pi_history(SESSION, sessions_dir=sessions)
    assert result.available and result.complete and not result.events
    assert path.exists()

    with pytest.raises(ValueError):
        pi_history.read_pi_history("../fixture", sessions_dir=sessions)


def test_import_envelope_split_across_text_blocks_is_flagged_as_one_message(tmp_path):
    marker = render_short_prompt(
        "Read the fixture context.", "fixture-import", ["codex"], 0, 1
    )
    blocks = [{"type": "text", "text": line} for line in marker.splitlines()]
    records = [
        header(),
        message("user-import", None, {"role": "user", "content": blocks}),
    ]
    sessions, _ = session_file(tmp_path, records)

    result = pi_history.read_pi_history(SESSION, sessions_dir=sessions)

    assert result.available and result.complete
    assert [event.event_id for event in result.events] == [
        "user-import",
        "user-import:1",
        "user-import:2",
    ]
    assert all(event.is_import for event in result.events)
    assert {event.import_id for event in result.events} == {"fixture-import"}
    assert "\n".join(event.text for event in result.events) == marker


def test_bash_execution_bookkeeping_and_unknown_entries_are_explicit(tmp_path):
    records = [
        header(),
        message("user-one", None, {"role": "user", "content": "Run the fixture"}),
        message(
            "bash-one",
            "user-one",
            {
                "role": "bashExecution",
                "command": "pwd",
                "output": "/fixture",
                "exitCode": 0,
                "cancelled": False,
                "truncated": True,
                "fullOutputPath": "/fixture/full.txt",
            },
        ),
        entry("custom-one", "bash-one", "custom", customType="fixture", data={}),
        entry("future-one", "custom-one", "future_entry"),
    ]
    sessions, _ = session_file(tmp_path, records)

    result = pi_history.read_pi_history(SESSION, sessions_dir=sessions)

    assert result.available and result.complete
    assert [event.event_id for event in result.events] == ["user-one", "bash-one"]
    assert result.events[1].render() == "Tool call (bash): pwd\n\nOutput:\n/fixture"
    assert set(result.unsupported) == {"truncated bash output", "unknown content"}


def test_a_session_header_after_the_first_record_is_not_a_match(tmp_path):
    records = [
        message("user-one", None, {"role": "user", "content": "Fixture question"}),
        header(),
    ]
    sessions, _ = session_file(tmp_path, records)

    result = pi_history.read_pi_history(SESSION, sessions_dir=sessions)

    assert result.missing and not result.events


def test_session_header_id_disambiguates_suffix_matches(tmp_path):
    longer = f"b_{SESSION}"
    sessions, target = session_file(
        tmp_path,
        [
            header(),
            message("target-one", None, {"role": "user", "content": "Target"}),
        ],
    )
    _, other = session_file(
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
    missing = pi_history.read_pi_history(SESSION, sessions_dir=sessions)

    assert missing.missing and not missing.events

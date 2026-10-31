import importlib
import json
import os
from pathlib import Path

import pytest

from kanbeat import claude_history
from kanbeat.session_transfer import render_short_prompt


def record(uuid, role, content, **extra):
    return {
        "uuid": uuid,
        "parentUuid": None,
        "sessionId": "fixture-session",
        "isSidechain": False,
        "type": role,
        "message": {"role": role, "content": content},
        **extra,
    }


def transcript(tmp_path, records):
    projects = tmp_path / "projects"
    directory = projects / "-fixture-project"
    directory.mkdir(parents=True)
    path = directory / "fixture-session.jsonl"
    path.write_text("".join(json.dumps(item) + "\n" for item in records))
    return projects, path


def test_real_shaped_history_order_roles_tools_imports_and_read_only(
    tmp_path, capsys, caplog
):
    marker = render_short_prompt(
        "Read the context file.", "fixture-import", ["codex"], 0, 1
    )
    records = [
        {"type": "system", "subtype": "turn_duration"},
        record("user-one", "user", "Fixture question", timestamp="2030-01-02"),
        record(
            "assistant-one",
            "assistant",
            [
                {"type": "thinking", "thinking": "PRIVATE_FIXTURE_REASONING"},
                {"type": "text", "text": "Fixture answer"},
                {
                    "type": "tool_use",
                    "id": "call-one",
                    "name": "Read",
                    "input": {"path": "fixture.txt"},
                },
            ],
            timestamp="2030-01-01",
        ),
        record(
            "tool-one",
            "user",
            [
                {
                    "type": "tool_result",
                    "tool_use_id": "call-one",
                    "content": [
                        {"type": "text", "text": "Fixture result"},
                        {"type": "image", "source": {"data": "PRIVATE_FIXTURE_IMAGE"}},
                        {"type": "tool_reference", "tool_name": "Read"},
                    ],
                }
            ],
        ),
        record("sidechain", "assistant", "PRIVATE_FIXTURE_SIDECHAIN", isSidechain=True),
        record("metadata", "user", "PRIVATE_FIXTURE_METADATA", isMeta=True),
        record("summary", "user", "PRIVATE_FIXTURE_SUMMARY", isCompactSummary=True),
        record("import-one", "user", marker),
        {"type": "attachment", "attachment": {"content": "PRIVATE_FIXTURE_ATTACHMENT"}},
    ]
    projects, path = transcript(tmp_path, records)
    before = path.read_bytes(), path.stat().st_mtime_ns
    result = claude_history.read_claude_history(
        "fixture-session", projects_dir=projects, ordinal_start=10
    )
    again = claude_history.read_claude_history(
        "fixture-session", projects_dir=projects, ordinal_start=10
    )
    assert result.available and result.complete
    assert [event.role for event in result.events] == [
        "user",
        "assistant",
        "tool_call",
        "tool_result",
        "user",
    ]
    assert [event.event_id for event in result.events] == [
        "user-one",
        "assistant-one",
        "assistant-one:1",
        "tool-one",
        "import-one",
    ]
    assert [event.position for event in result.events] == [1, 2, 2, 3, 7]
    assert [event.ordinal for event in result.events] == list(range(10, 15))
    assert result.events == again.events
    assert result.events[2].label == "Tool call (Read)"
    assert result.events[3].label == "Tool result (Read)"
    assert result.events[3].text == "Fixture result"
    assert result.events[-1].is_import
    assert result.events[-1].import_id == "fixture-import"
    assert set(result.unsupported) == {
        "hidden reasoning",
        "images",
        "content block: tool_reference",
        "compaction summaries",
        "attachments",
    }
    assert "PRIVATE_FIXTURE" not in repr(result.events)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert set(projects.rglob("*")) == {path.parent, path}
    captured = capsys.readouterr()
    assert not captured.out
    assert not captured.err
    assert not caplog.records


def test_config_directory_and_lazy_import(tmp_path, monkeypatch):
    projects, path = transcript(tmp_path, [record("one", "user", "Fixture")])
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(projects.parent))
    with monkeypatch.context() as patch:

        def forbidden(*args, **kwargs):
            raise AssertionError("History accessed during module import")

        patch.setattr(Path, "open", forbidden)
        patch.setattr(Path, "glob", forbidden)
        patch.setattr(os, "scandir", forbidden)
        importlib.reload(claude_history)
    result = claude_history.read_claude_history("fixture-session")
    assert result.available and result.transcript == path


def test_missing_and_read_failure_are_distinct(tmp_path, monkeypatch):
    projects, path = transcript(tmp_path, [record("one", "user", "Fixture")])
    missing = claude_history.read_claude_history("absent", projects_dir=projects)
    assert missing.missing and not missing.complete and not missing.events
    original = Path.open

    def denied(self, *args, **kwargs):
        if self == path:
            raise PermissionError("fixture access denied")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    result = claude_history.read_claude_history(
        "fixture-session", projects_dir=projects
    )
    assert result.unreachable and not result.complete and not result.events


def test_unreadable_project_directory_is_unreachable(tmp_path, monkeypatch):
    projects, path = transcript(tmp_path, [record("one", "user", "Fixture")])
    original = os.scandir

    def denied(directory):
        if Path(directory) == path.parent:
            raise PermissionError("fixture project temporarily inaccessible")
        return original(directory)

    monkeypatch.setattr(os, "scandir", denied)
    result = claude_history.read_claude_history(
        "fixture-session", projects_dir=projects
    )
    assert result.unreachable
    assert not result.missing


def test_unreadable_projects_root_is_unreachable(tmp_path, monkeypatch):
    projects, _ = transcript(tmp_path, [])
    original = os.scandir

    def denied(directory):
        if Path(directory) == projects:
            raise PermissionError("fixture projects temporarily inaccessible")
        return original(directory)

    monkeypatch.setattr(os, "scandir", denied)
    result = claude_history.read_claude_history(
        "fixture-session", projects_dir=projects
    )
    assert result.unreachable and not result.missing


def test_unreadable_unrelated_project_does_not_hide_available_history(
    tmp_path, monkeypatch
):
    projects, path = transcript(tmp_path, [record("one", "user", "Fixture")])
    unrelated = projects / "-unrelated-project"
    unrelated.mkdir()
    original = os.scandir

    def denied(directory):
        if Path(directory) == unrelated:
            raise PermissionError("fixture unrelated project inaccessible")
        return original(directory)

    monkeypatch.setattr(os, "scandir", denied)
    result = claude_history.read_claude_history(
        "fixture-session", projects_dir=projects
    )
    assert result.available and result.complete and result.transcript == path


@pytest.mark.parametrize("bad_line", [b"{partial", b"\xff\n", b"[]\n"])
def test_malformed_history_is_available_but_incomplete(tmp_path, bad_line):
    projects, path = transcript(tmp_path, [record("one", "user", "Fixture")])
    with path.open("ab") as handle:
        handle.write(bad_line)
    result = claude_history.read_claude_history(
        "fixture-session", projects_dir=projects
    )
    assert result.available and not result.complete
    assert len(result.events) == 1


def test_empty_history_is_available(tmp_path):
    projects, _ = transcript(tmp_path, [])
    result = claude_history.read_claude_history(
        "fixture-session", projects_dir=projects
    )
    assert result.available and result.complete and not result.events

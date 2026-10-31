import asyncio
import json
from types import SimpleNamespace

import pytest

from kanbeat.agent_cli import (
    config_command,
    diff_events,
    format_event,
    read_value,
    status_command,
    watch_command,
    watch_index,
)
from kanbeat.supervisor import Supervisor


def args(**values):
    return SimpleNamespace(**values)


@pytest.fixture
def supervisor(tmp_path):
    return Supervisor(tmp_path)


def test_config_get_set_history_and_revert_through_the_commands(supervisor, capsys):
    async def scenario():
        await config_command(
            supervisor.dispatch,
            args(config_command="set", key="global_cp", value="Be brief", file=None),
        )
        await config_command(
            supervisor.dispatch, args(config_command="get", key="global_cp")
        )
        await config_command(
            supervisor.dispatch, args(config_command="get", key="max_parallel")
        )
        await config_command(supervisor.dispatch, args(config_command="get", key=None))
        await config_command(
            supervisor.dispatch, args(config_command="history", limit=5)
        )
        await config_command(supervisor.dispatch, args(config_command="revert", id=1))
        await config_command(
            supervisor.dispatch,
            args(config_command="set", key="max_parallel", value="3", file=None),
        )
        await config_command(
            supervisor.dispatch,
            args(config_command="set", key="max_parallel", value="3", file=None),
        )

    asyncio.run(scenario())
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "changed: global_cp (change #1)"
    assert lines[1] == "Be brief"
    assert lines[2] == "3"
    assert (
        json.loads("\n".join(lines[3:])[: "\n".join(lines[3:]).index("}\n#") + 1])[
            "global_cp"
        ]
        == "Be brief"
    )
    text = "\n".join(lines)
    assert "#1  " in text and "global_cp: '' -> 'Be brief'" in text
    assert "reverted: global_cp (change #2)" in text
    assert "Nothing to change" in lines[-1]


def test_a_value_comes_from_the_argument_a_file_or_standard_input(
    tmp_path, monkeypatch
):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Line one\nLine two\n", encoding="utf-8")
    assert read_value("x", None) == "x"
    assert read_value(None, str(prompt)) == "Line one\nLine two\n"
    monkeypatch.setattr("sys.stdin", SimpleNamespace(read=lambda: "from stdin"))
    assert read_value("-", None) == "from stdin"
    with pytest.raises(ValueError, match="either a value or --file"):
        read_value("x", str(prompt))
    with pytest.raises(ValueError, match="Give a value"):
        read_value(None, None)


def test_status_lists_columns_with_blockers_and_json_form(supervisor, capsys):
    first = supervisor.board.create("First task", "", "todo")
    supervisor.board.create("Second task", "Blocked by: #" + first.id, "backlog")

    async def scenario():
        await status_command(supervisor.dispatch, False)
        await status_command(supervisor.dispatch, True)

    asyncio.run(scenario())
    output = capsys.readouterr().out
    text, _, raw = output.partition("{")
    assert "execution: stopped" in text
    assert f"todo (1)\n  #{first.id} First task" in text
    assert "(blocked by #" + first.id + ")" in text
    data = json.loads("{" + raw)
    assert data["execution"] == "stopped"
    assert data["columns"]["todo"][0]["id"] == first.id
    assert data["columns"]["backlog"][0]["blocked_by"] == [first.id]


def snapshot(execution="stopped", **tickets):
    return {
        "execution": execution,
        "tickets": [
            {"id": ticket_id, "column": column, "title": "Title " + ticket_id}
            for ticket_id, column in tickets.items()
        ],
    }


def test_events_describe_created_moved_deleted_and_execution_changes():
    before = watch_index(snapshot(t001="todo", t002="progress", t003="done"))
    after = watch_index(
        snapshot("running", t001="progress", t002="progress", t004="backlog")
    )
    events = diff_events(before, after)
    assert [event["event"] for event in events] == [
        "moved",
        "deleted",
        "created",
        "execution",
    ]
    assert [format_event(event) for event in events] == [
        "moved #t001 todo -> progress  Title t001",
        "deleted #t003  Title t003",
        "created #t004 in backlog  Title t004",
        "execution stopped -> running",
    ]
    assert diff_events(before, before) == []


def test_watch_prints_one_line_per_event_as_text_or_json(capsys):
    states = iter(
        [
            snapshot(a="todo"),
            snapshot(a="todo"),
            snapshot("running", a="progress"),
        ]
    )

    async def send(request):
        return next(states)

    asyncio.run(watch_command(send, False, 0, rounds=2))
    assert capsys.readouterr().out.splitlines() == [
        "moved #a todo -> progress  Title a",
        "execution stopped -> running",
    ]
    states = iter([snapshot(a="todo"), snapshot(a="done")])
    asyncio.run(watch_command(send, True, 0, rounds=1))
    [line] = capsys.readouterr().out.splitlines()
    event = json.loads(line)
    assert event["event"] == "moved" and event["to"] == "done"
    assert event["time"].endswith("Z")


def test_watch_reports_a_lost_supervisor_and_picks_up_after_it_returns(capsys):
    calls = iter(
        [
            snapshot(a="todo"),
            RuntimeError("down"),
            OSError("gone"),
            snapshot("running", a="progress"),
        ]
    )

    async def send(request):
        item = next(calls)
        if isinstance(item, Exception):
            raise item
        return item

    asyncio.run(watch_command(send, False, 0, rounds=3))
    assert capsys.readouterr().out.splitlines() == [
        "supervisor disconnected",
        "supervisor connected",
        "moved #a todo -> progress  Title a",
        "execution stopped -> running",
    ]


def test_watch_gives_up_when_the_supervisor_never_comes_back(capsys):
    state = {"calls": 0}

    async def send(request):
        state["calls"] += 1
        if state["calls"] > 1:
            raise OSError("gone")
        return snapshot(a="todo")

    with pytest.raises(RuntimeError, match="stopped answering"):
        asyncio.run(watch_command(send, True, 0, retries=3))
    lines = capsys.readouterr().out.splitlines()
    assert [json.loads(line)["event"] for line in lines] == ["disconnected"]


def test_watch_retries_when_the_first_answer_fails_or_is_malformed(capsys):
    steps = iter(
        [
            OSError("not up yet"),
            {"tickets": "broken"},
            snapshot(a="todo"),
            snapshot(a="progress"),
        ]
    )

    async def send(request):
        item = next(steps)
        if isinstance(item, Exception):
            raise item
        return item

    asyncio.run(watch_command(send, False, 0, rounds=3))
    assert capsys.readouterr().out.splitlines() == [
        "supervisor disconnected",
        "supervisor connected",
        "moved #a todo -> progress  Title a",
    ]

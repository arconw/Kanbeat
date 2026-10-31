import asyncio
import json
from types import SimpleNamespace

import pytest

from kanbeat.agent_cli import (
    config_command,
    events_command,
    read_value,
    status_command,
    watch_command,
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


def event(seq, kind="moved", **fields):
    base = {"seq": seq, "time": "2026-10-31T10:00:00Z", "event": kind}
    return {**base, **fields}


def moved(seq, ticket="a"):
    return event(
        seq,
        ticket=ticket,
        **{"from": "todo", "to": "progress"},
        title="Title " + ticket,
    )


def test_events_prints_what_came_after_a_number_and_returns_the_last(capsys):
    async def send(request):
        assert request == {"op": "events", "since": 4, "limit": 50}
        return {
            "events": [
                moved(5),
                event(6, "execution", **{"from": "stopped", "to": "running"}),
            ],
            "last": 6,
        }

    assert asyncio.run(events_command(send, False, 4, 50)) == 6
    assert capsys.readouterr().out.splitlines() == [
        "moved #a todo -> progress  Title a",
        "execution stopped -> running",
    ]


def test_watch_starts_now_then_prints_new_events_as_text_or_json(capsys):
    replies = iter(
        [
            {"events": [], "last": 7},
            {"events": [], "last": 7},
            {"events": [moved(8)], "last": 8},
        ]
    )
    asked = []

    async def send(request):
        asked.append(request["since"])
        return next(replies)

    asyncio.run(watch_command(send, False, 0, rounds=2))
    assert asked == [None, 7, 7]
    assert capsys.readouterr().out.splitlines() == [
        "moved #a todo -> progress  Title a"
    ]
    replies = iter(
        [{"events": [moved(3)], "last": 3}, {"events": [moved(4, "b")], "last": 4}]
    )
    asyncio.run(watch_command(send, True, 0, since=2, rounds=1))
    first, second = (json.loads(line) for line in capsys.readouterr().out.splitlines())
    assert (first["seq"], first["ticket"], second["seq"], second["ticket"]) == (
        3,
        "a",
        4,
        "b",
    )
    assert first["time"] == "2026-10-31T10:00:00Z"


def test_watch_reports_a_lost_supervisor_and_continues_from_its_cursor(capsys):
    steps = iter(
        [
            {"events": [], "last": 2},
            RuntimeError("down"),
            OSError("gone"),
            {"events": [moved(3), moved(4, "b")], "last": 4},
        ]
    )
    asked = []

    async def send(request):
        asked.append(request["since"])
        item = next(steps)
        if isinstance(item, Exception):
            raise item
        return item

    asyncio.run(watch_command(send, False, 0, rounds=3))
    assert asked == [None, 2, 2, 2]
    assert capsys.readouterr().out.splitlines() == [
        "supervisor disconnected",
        "supervisor connected",
        "moved #a todo -> progress  Title a",
        "moved #b todo -> progress  Title b",
    ]


def test_watch_gives_up_when_the_supervisor_never_comes_back(capsys):
    state = {"calls": 0}

    async def send(request):
        state["calls"] += 1
        if state["calls"] > 1:
            raise OSError("gone")
        return {"events": [], "last": 0}

    with pytest.raises(RuntimeError, match="stopped answering"):
        asyncio.run(watch_command(send, True, 0, retries=3))
    assert [
        json.loads(line)["event"] for line in capsys.readouterr().out.splitlines()
    ] == ["disconnected"]


def test_watch_retries_when_the_first_answer_fails_or_is_malformed(capsys):
    steps = iter(
        [
            OSError("not up yet"),
            {"nope": 1},
            {"events": [], "last": 5},
            {"events": [moved(6)], "last": 6},
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


def test_watch_starts_over_when_the_supervisor_numbers_events_from_scratch(capsys):
    steps = iter(
        [
            {"events": [], "last": 40},
            {"events": [], "last": 2},
            {"events": [moved(3)], "last": 3},
        ]
    )

    async def send(request):
        return next(steps)

    asyncio.run(watch_command(send, False, 0, rounds=2))
    assert capsys.readouterr().out.splitlines() == [
        "moved #a todo -> progress  Title a"
    ]


def test_events_without_a_number_asks_for_the_newest_ones(capsys):
    async def send(request):
        assert request == {"op": "events", "tail": 3}
        return {"events": [moved(8), moved(9, "b")], "last": 9}

    assert asyncio.run(events_command(send, False, None, 3)) == 9
    assert capsys.readouterr().out.splitlines() == [
        "moved #a todo -> progress  Title a",
        "moved #b todo -> progress  Title b",
    ]


def test_commands_say_when_older_events_are_gone(capsys):
    async def send(request):
        return {"events": [moved(9)], "last": 9, "missed": 4}

    asyncio.run(events_command(send, False, 2, 10))
    assert capsys.readouterr().out.splitlines() == [
        "4 earlier events are no longer available",
        "moved #a todo -> progress  Title a",
    ]
    replies = iter(
        [
            {"events": [], "last": 20},
            {"events": [moved(30)], "last": 30, "missed": 9},
        ]
    )

    async def watching(request):
        return next(replies)

    asyncio.run(watch_command(watching, True, 0, since=None, rounds=1))
    gap, event = (json.loads(line) for line in capsys.readouterr().out.splitlines())
    assert gap["event"] == "gap" and gap["missed"] == 9 and event["seq"] == 30

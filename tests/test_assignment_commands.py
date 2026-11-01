import asyncio
import json
from types import SimpleNamespace

import pytest

from kanbeat.agent_cli import (
    RESTART_HELP,
    assign_command,
    board_control_command,
    check_harness,
    check_reviewer,
    check_slot,
    reviewer_number,
    reviewers_command,
    status_command,
)
from kanbeat.cli import parser
from kanbeat.filesystem import (
    INSTRUCTION,
    LEGACY_INSTRUCTION_HASHES,
    assignment_change_blocked,
    assignment_change_refusal,
)
from kanbeat.supervisor import Supervisor

PREVIOUS_INSTRUCTIONS_SHA256 = (
    "a71c706fecbfbd748e9e6bd120abc3af9873e8e98fc692f4d74d510b533ce54d"
)
SLOT_RULE_INSTRUCTIONS_SHA256 = (
    "0fee5c9f9e2bf94972847ba0200b183695859a02876c964a3734f9d8a7359a04"
)


def args(**values):
    return SimpleNamespace(**values)


def ticket_in_column(supervisor, column):
    ticket = supervisor.board.create(
        "Editable", column="backlog" if column == "archive" else column
    )
    if column == "archive":
        ticket = supervisor.board.move(ticket.id, "archive")
    return ticket


def test_parser_accepts_assignments_and_shows_the_restart_help(capsys):
    p = parser()
    assigned = p.parse_args(
        ["assign", "004", "005", "--slot", "reviewer-1", "--harness", "codex"]
    )
    assert assigned.command == "assign"
    assert assigned.tickets == ["004", "005"]
    assert assigned.slot == "reviewer-1"
    assert assigned.harness == "codex"
    assert assigned.restart is False
    defaults = p.parse_args(["assign", "001", "--harness", "claude"])
    assert defaults.slot == "developer"
    assert defaults.cp is None and defaults.cp_file is None
    added = p.parse_args(["reviewers", "add", "004", "--harness", "codex", "--restart"])
    assert added.reviewers_command == "add"
    assert added.ticket == "004"
    assert added.restart is True
    removed = p.parse_args(["reviewers", "remove", "004", "reviewer-2"])
    assert removed.reviewers_command == "remove"
    assert removed.slot == "reviewer-2"
    assert "resume from their native sessions" in RESTART_HELP
    with pytest.raises(SystemExit):
        p.parse_args(["assign", "--help"])
    assert "resume from their native sessions" in capsys.readouterr().out


def test_slot_and_harness_validation_names_the_valid_values():
    assert check_slot("developer") == "developer"
    assert check_slot("reviewer-12") == "reviewer-12"
    assert check_reviewer("reviewer-1") == "reviewer-1"
    with pytest.raises(ValueError, match="Invalid slot"):
        check_slot("reviewer-x")
    with pytest.raises(ValueError, match="Invalid slot"):
        check_slot("")
    with pytest.raises(ValueError, match="not a Reviewer slot"):
        check_reviewer("developer")
    with pytest.raises(ValueError) as info:
        check_harness("gpt")
    for name in ("codex", "claude", "opencode", "pi"):
        assert name in str(info.value)


def test_non_ascii_and_zero_padded_reviewer_slots_are_rejected(tmp_path):
    for slot in ("reviewer-", "reviewer-0", "reviewer-01", "reviewer-\u00b2"):
        with pytest.raises(ValueError, match="Invalid slot"):
            check_slot(slot)
        assert reviewer_number(slot) is None
    supervisor = Supervisor(tmp_path)
    ticket = supervisor.board.create("Reviewed", column="review")
    with pytest.raises(ValueError, match="Invalid reviewer slot identifier"):
        asyncio.run(
            supervisor.dispatch(
                {
                    "op": "set_assignment",
                    "ticket": ticket.id,
                    "slot": "reviewer-01",
                    "values": {"harness": "codex"},
                }
            )
        )
    assert supervisor.ticket_state(ticket.id)["reviewers"] == []


def test_assign_sets_several_reviewers_and_status_reports_them(tmp_path, capsys):
    supervisor = Supervisor(tmp_path)
    first = supervisor.board.create("First", column="review")
    second = supervisor.board.create("Second", column="review")

    async def scenario():
        await assign_command(
            supervisor.dispatch,
            args(
                tickets=[first.id, second.id],
                slot="reviewer-1",
                harness="codex",
                cp=None,
                cp_file=None,
                restart=False,
            ),
        )
        await status_command(supervisor.dispatch, True)

    asyncio.run(scenario())
    output = capsys.readouterr().out
    text, _, raw = output.partition("{")
    assert f"assigned reviewer-1 on #{first.id}: harness codex" in text
    assert f"assigned reviewer-1 on #{second.id}: harness codex" in text
    data = json.loads("{" + raw)
    rows = {row["id"]: row for row in data["columns"]["review"]}
    assert rows[first.id]["slots"]["reviewers"] == [
        {
            "slot": "reviewer-1",
            "harness": "codex",
            "custom_prompt": False,
            "running": False,
        }
    ]
    assert (
        rows[first.id]["slots"]["developer"]["harness"]
        == (supervisor.runtime.config["default_harness"])
    )
    assert rows[first.id]["straight_to_done"] is False
    assert rows[second.id]["slots"]["reviewers"][0]["harness"] == "codex"


def test_status_text_marks_no_reviewer_and_reports_running_workers(tmp_path, capsys):
    supervisor = Supervisor(tmp_path)
    plain = supervisor.board.create("Plain", column="todo")
    reviewed = supervisor.board.create("Reviewed", column="progress")
    state = supervisor.ticket_state(reviewed.id)
    state["developer"] = {"harness": "pi", "cp": "Be brief"}
    state["reviewers"] = [{"id": "reviewer-1", "harness": "claude"}]
    state["runtime"] = {"reviewer-1": {"status": "running"}}
    supervisor.save()

    asyncio.run(status_command(supervisor.dispatch, False))
    lines = capsys.readouterr().out.splitlines()
    plain_index = lines.index(f"  #{plain.id} Plain")
    assert lines[plain_index + 1] == (
        "    developer: harness "
        + supervisor.runtime.config["default_harness"]
        + ", no custom prompt, no worker"
    )
    assert (
        lines[plain_index + 2]
        == "    no Reviewer slot: goes straight to Done when development finishes"
    )
    reviewed_index = lines.index(f"  #{reviewed.id} Reviewed")
    assert lines[reviewed_index + 1] == (
        "    developer: harness pi, custom prompt, no worker"
    )
    assert lines[reviewed_index + 2] == (
        "    reviewer-1: harness claude, no custom prompt, worker running"
    )

    asyncio.run(status_command(supervisor.dispatch, True))
    data = json.loads(capsys.readouterr().out)
    rows = {row["id"]: row for row in data["columns"]["todo"]}
    assert rows[plain.id]["straight_to_done"] is True
    assert rows[plain.id]["slots"]["developer"]["custom_prompt"] is False
    rows = {row["id"]: row for row in data["columns"]["progress"]}
    assert rows[reviewed.id]["slots"]["developer"]["custom_prompt"] is True
    assert rows[reviewed.id]["slots"]["reviewers"][0]["running"] is True
    assert rows[reviewed.id]["straight_to_done"] is False


def test_assign_reads_a_custom_prompt_from_a_file_or_standard_input(
    tmp_path, monkeypatch, capsys
):
    supervisor = Supervisor(tmp_path)
    ticket = supervisor.board.create("Reviewed", column="review")
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Line one\nLine two\n", encoding="utf-8")

    async def scenario():
        await assign_command(
            supervisor.dispatch,
            args(
                tickets=[ticket.id],
                slot="reviewer-1",
                harness="codex",
                cp=None,
                cp_file=str(prompt),
                restart=False,
            ),
        )
        monkeypatch.setattr(
            "sys.stdin", SimpleNamespace(read=lambda: "From standard input")
        )
        await assign_command(
            supervisor.dispatch,
            args(
                tickets=[ticket.id],
                slot="reviewer-2",
                harness="claude",
                cp="-",
                cp_file=None,
                restart=False,
            ),
        )

    asyncio.run(scenario())
    reviewers = {
        row["id"]: row for row in supervisor.ticket_state(ticket.id)["reviewers"]
    }
    assert reviewers["reviewer-1"]["cp"] == "Line one\nLine two\n"
    assert reviewers["reviewer-2"]["cp"] == "From standard input"
    with pytest.raises(ValueError, match="either a value or --cp-file"):
        asyncio.run(
            assign_command(
                supervisor.dispatch,
                args(
                    tickets=[ticket.id],
                    slot="reviewer-1",
                    harness="codex",
                    cp="Text",
                    cp_file=str(prompt),
                    restart=False,
                ),
            )
        )


def test_assign_clears_the_old_verdict_when_the_harness_changes(tmp_path):
    supervisor = Supervisor(tmp_path)
    ticket = supervisor.board.create("Reviewed", column="review")
    state = supervisor.ticket_state(ticket.id)
    state["reviewers"] = [
        {"id": "reviewer-1", "harness": "pi", "session": "old-session"}
    ]
    state["review_results"] = {"reviewer-1": {"verdict": "approve"}}
    state["runtime"] = {"reviewer-1": {"status": "completed"}}
    supervisor.save()

    asyncio.run(
        assign_command(
            supervisor.dispatch,
            args(
                tickets=[ticket.id],
                slot="reviewer-1",
                harness="claude",
                cp=None,
                cp_file=None,
                restart=False,
            ),
        )
    )
    state = supervisor.ticket_state(ticket.id)
    assert state["reviewers"] == [{"id": "reviewer-1", "harness": "claude"}]
    assert state["review_results"] == {}
    assert state["runtime"] == {}


def test_reviewers_add_takes_the_next_free_slot_and_remove_warns_on_the_last(
    tmp_path, capsys
):
    supervisor = Supervisor(tmp_path)
    ticket = supervisor.board.create("Reviewed", column="review")

    async def scenario():
        await reviewers_command(
            supervisor.dispatch,
            args(
                reviewers_command="add",
                ticket=ticket.id,
                harness="codex",
                restart=False,
            ),
        )
        await reviewers_command(
            supervisor.dispatch,
            args(
                reviewers_command="add",
                ticket=ticket.id,
                harness="claude",
                restart=False,
            ),
        )
        await reviewers_command(
            supervisor.dispatch,
            args(
                reviewers_command="remove",
                ticket=ticket.id,
                slot="reviewer-1",
                restart=False,
            ),
        )
        await reviewers_command(
            supervisor.dispatch,
            args(
                reviewers_command="add",
                ticket=ticket.id,
                harness="pi",
                restart=False,
            ),
        )
        await reviewers_command(
            supervisor.dispatch,
            args(
                reviewers_command="remove",
                ticket=ticket.id,
                slot="reviewer-2",
                restart=False,
            ),
        )
        await reviewers_command(
            supervisor.dispatch,
            args(
                reviewers_command="remove",
                ticket=ticket.id,
                slot="reviewer-1",
                restart=False,
            ),
        )

    asyncio.run(scenario())
    lines = capsys.readouterr().out.splitlines()
    assert f"assigned reviewer-1 on #{ticket.id}: harness codex" in lines
    assert f"assigned reviewer-2 on #{ticket.id}: harness claude" in lines
    assert f"removed reviewer-1 from #{ticket.id}" in lines
    assert f"assigned reviewer-1 on #{ticket.id}: harness pi" in lines
    assert f"removed reviewer-2 from #{ticket.id}" in lines
    assert lines[-1] == (
        f"No Reviewer left on #{ticket.id}: it goes straight to Done when its "
        "development finishes."
    )
    assert supervisor.ticket_state(ticket.id)["reviewers"] == []


def test_reviewers_add_refuses_a_slot_that_appeared_after_the_snapshot(tmp_path):
    supervisor = Supervisor(tmp_path)
    ticket = supervisor.board.create("Reviewed", column="review")

    async def racing_send(request):
        if request["op"] == "set_assignment" and request.get("only_if_absent"):
            await supervisor.dispatch(
                {
                    "op": "set_assignment",
                    "ticket": ticket.id,
                    "slot": request["slot"],
                    "values": {"harness": "claude"},
                }
            )
        return await supervisor.dispatch(request)

    with pytest.raises(ValueError, match="already exists"):
        asyncio.run(
            reviewers_command(
                racing_send,
                args(
                    reviewers_command="add",
                    ticket=ticket.id,
                    harness="codex",
                    restart=False,
                ),
            )
        )
    assert supervisor.ticket_state(ticket.id)["reviewers"] == [
        {"id": "reviewer-1", "harness": "claude"}
    ]


def test_remove_reviewer_refuses_a_running_terminal_and_clears_slot_state(tmp_path):
    supervisor = Supervisor(tmp_path)
    ticket = supervisor.board.create("Reviewed", column="review")
    asyncio.run(
        supervisor.dispatch(
            {
                "op": "set_assignment",
                "ticket": ticket.id,
                "slot": "reviewer-1",
                "values": {"harness": "codex"},
            }
        )
    )
    state = supervisor.ticket_state(ticket.id)
    state["review_results"] = {"reviewer-1": {"verdict": "approve"}}
    state["runtime"] = {"reviewer-1": {"status": "completed"}}
    supervisor.save()
    supervisor.terminals.entries["terminal"] = {
        "ticket": ticket.id,
        "slot": "reviewer-1",
        "status": "running",
        "owner": "board",
    }
    with pytest.raises(ValueError, match="interactive slot terminal"):
        asyncio.run(
            supervisor.dispatch(
                {
                    "op": "remove_reviewer",
                    "ticket": ticket.id,
                    "slot": "reviewer-1",
                }
            )
        )
    state = supervisor.ticket_state(ticket.id)
    assert [row["id"] for row in state["reviewers"]] == ["reviewer-1"]
    assert "reviewer-1" in state["review_results"]
    assert "reviewer-1" in state["runtime"]

    supervisor.terminals.entries["terminal"]["status"] = "interrupted"
    asyncio.run(
        supervisor.dispatch(
            {"op": "remove_reviewer", "ticket": ticket.id, "slot": "reviewer-1"}
        )
    )
    state = supervisor.ticket_state(ticket.id)
    assert state["reviewers"] == []
    assert state["review_results"] == {}
    assert state["runtime"] == {}

    with pytest.raises(ValueError, match="Invalid reviewer slot identifier"):
        asyncio.run(
            supervisor.dispatch(
                {"op": "remove_reviewer", "ticket": ticket.id, "slot": "developer"}
            )
        )


def test_reviewers_remove_refuses_a_slot_the_ticket_does_not_have(tmp_path):
    supervisor = Supervisor(tmp_path)
    ticket = supervisor.board.create("Reviewed", column="review")
    with pytest.raises(ValueError, match=f"#{ticket.id} has no reviewer-9"):
        asyncio.run(
            reviewers_command(
                supervisor.dispatch,
                args(
                    reviewers_command="remove",
                    ticket=ticket.id,
                    slot="reviewer-9",
                    restart=False,
                ),
            )
        )


def test_the_commands_refuse_a_running_board_and_name_stop_and_play(tmp_path):
    supervisor = Supervisor(tmp_path)
    ticket = supervisor.board.create("Reviewed", column="review")
    asyncio.run(
        supervisor.dispatch(
            {
                "op": "set_assignment",
                "ticket": ticket.id,
                "slot": "reviewer-1",
                "values": {"harness": "codex"},
            }
        )
    )
    supervisor.runtime.data["execution"] = "running"
    before = json.dumps(supervisor.snapshot()["tickets"], sort_keys=True)

    requests = [
        (
            assign_command,
            args(
                tickets=[ticket.id],
                slot="reviewer-1",
                harness="claude",
                cp=None,
                cp_file=None,
                restart=False,
            ),
        ),
        (
            reviewers_command,
            args(
                reviewers_command="add",
                ticket=ticket.id,
                harness="claude",
                restart=False,
            ),
        ),
        (
            reviewers_command,
            args(
                reviewers_command="remove",
                ticket=ticket.id,
                slot="reviewer-1",
                restart=False,
            ),
        ),
    ]
    for command, arguments in requests:
        with pytest.raises(ValueError) as info:
            asyncio.run(command(supervisor.dispatch, arguments))
        assert "kanbeat stop" in str(info.value)
        assert "kanbeat play" in str(info.value)

    with pytest.raises(ValueError) as info:
        asyncio.run(
            supervisor.dispatch(
                {
                    "op": "set_assignment",
                    "ticket": ticket.id,
                    "slot": "reviewer-1",
                    "values": {"harness": "claude"},
                }
            )
        )
    assert "kanbeat stop" in str(info.value)
    assert "kanbeat play" in str(info.value)
    assert json.dumps(supervisor.snapshot()["tickets"], sort_keys=True) == before


def test_restart_stops_and_plays_only_when_the_board_was_running(tmp_path, monkeypatch):
    import kanbeat.supervisor as module

    monkeypatch.setattr(module, "available", lambda _name: True)
    supervisor = Supervisor(tmp_path)
    ticket = supervisor.board.create("Reviewed", column="review")
    operations = []

    async def send(request):
        operations.append(request["op"])
        return await supervisor.dispatch(request)

    supervisor.runtime.data["execution"] = "running"
    asyncio.run(
        assign_command(
            send,
            args(
                tickets=[ticket.id],
                slot="reviewer-1",
                harness="codex",
                cp=None,
                cp_file=None,
                restart=True,
            ),
        )
    )
    assert operations == ["snapshot", "stop", "set_assignment", "play"]
    assert supervisor.runtime.data["execution"] == "running"
    assert supervisor.ticket_state(ticket.id)["reviewers"] == [
        {"id": "reviewer-1", "harness": "codex"}
    ]

    operations.clear()
    supervisor.runtime.data["execution"] = "stopped"
    asyncio.run(
        reviewers_command(
            send,
            args(
                reviewers_command="add",
                ticket=ticket.id,
                harness="claude",
                restart=True,
            ),
        )
    )
    assert operations == ["snapshot", "set_assignment"]
    assert supervisor.runtime.data["execution"] == "stopped"
    assert [row["id"] for row in supervisor.ticket_state(ticket.id)["reviewers"]] == [
        "reviewer-1",
        "reviewer-2",
    ]


def test_restart_replays_a_board_it_stopped_when_a_change_is_refused(
    tmp_path, monkeypatch
):
    import kanbeat.supervisor as module

    monkeypatch.setattr(module, "available", lambda _name: True)
    supervisor = Supervisor(tmp_path)
    first = supervisor.board.create("First", column="review")
    second = supervisor.board.create("Second", column="review")
    supervisor.runtime.data["execution"] = "running"
    operations = []

    async def send(request):
        operations.append(request["op"])
        if request["op"] == "set_assignment" and request["ticket"] == second.id:
            raise ValueError("Refused by the test")
        return await supervisor.dispatch(request)

    with pytest.raises(ValueError) as info:
        asyncio.run(
            assign_command(
                send,
                args(
                    tickets=[first.id, second.id],
                    slot="developer",
                    harness="codex",
                    cp=None,
                    cp_file=None,
                    restart=True,
                ),
            )
        )
    message = str(info.value)
    assert f"Changed #{first.id} before the change was refused" in message
    assert "Refused by the test" in message
    assert operations == [
        "snapshot",
        "stop",
        "set_assignment",
        "set_assignment",
        "play",
    ]
    assert supervisor.runtime.data["execution"] == "running"
    assert supervisor.ticket_state(first.id)["developer"] == {"harness": "codex"}
    assert not supervisor.ticket_state(second.id)["developer"]


def test_a_busy_slot_terminal_refuses_the_whole_batch_before_stopping(
    tmp_path, monkeypatch
):
    import kanbeat.supervisor as module

    monkeypatch.setattr(module, "available", lambda _name: True)
    supervisor = Supervisor(tmp_path)
    first = supervisor.board.create("First", column="review")
    second = supervisor.board.create("Second", column="review")
    supervisor.terminals.entries["terminal"] = {
        "ticket": second.id,
        "slot": "developer",
        "status": "running",
        "owner": "custom",
    }
    supervisor.runtime.data["execution"] = "running"
    operations = []

    async def send(request):
        operations.append(request["op"])
        return await supervisor.dispatch(request)

    with pytest.raises(ValueError) as info:
        asyncio.run(
            assign_command(
                send,
                args(
                    tickets=[first.id, second.id],
                    slot="developer",
                    harness="codex",
                    cp=None,
                    cp_file=None,
                    restart=True,
                ),
            )
        )
    message = str(info.value)
    assert f"#{second.id}/developer" in message
    assert "interactive slot terminal" in message
    assert operations == ["snapshot"]
    assert supervisor.runtime.data["execution"] == "running"
    assert not supervisor.ticket_state(first.id)["developer"]
    assert not supervisor.ticket_state(second.id)["developer"]


def test_refusing_a_slot_with_a_running_terminal_changes_nothing(tmp_path):
    supervisor = Supervisor(tmp_path)
    ticket = supervisor.board.create("Reviewed", column="review")
    supervisor.terminals.entries["terminal"] = {
        "ticket": ticket.id,
        "slot": "reviewer-1",
        "status": "running",
        "owner": "custom",
    }
    with pytest.raises(ValueError, match="interactive slot terminal"):
        asyncio.run(
            supervisor.dispatch(
                {
                    "op": "set_assignment",
                    "ticket": ticket.id,
                    "slot": "reviewer-1",
                    "values": {"harness": "codex"},
                }
            )
        )
    assert supervisor.ticket_state(ticket.id)["reviewers"] == []


def test_invalid_values_change_nothing(tmp_path):
    supervisor = Supervisor(tmp_path)
    ticket = supervisor.board.create("Reviewed", column="review")
    before = json.dumps(supervisor.snapshot()["tickets"], sort_keys=True)
    cases = [
        (
            dict(
                tickets=[ticket.id],
                slot="reviewer-1",
                harness="gpt",
                cp=None,
                cp_file=None,
                restart=False,
            ),
            "Unknown harness",
        ),
        (
            dict(
                tickets=[ticket.id],
                slot="reviewer-x",
                harness="codex",
                cp=None,
                cp_file=None,
                restart=False,
            ),
            "Invalid slot",
        ),
        (
            dict(
                tickets=["999"],
                slot="reviewer-1",
                harness="codex",
                cp=None,
                cp_file=None,
                restart=False,
            ),
            "Unknown ticket",
        ),
    ]
    for values, message in cases:
        with pytest.raises(ValueError, match=message):
            asyncio.run(assign_command(supervisor.dispatch, args(**values)))
    with pytest.raises(ValueError, match="not a Reviewer slot"):
        asyncio.run(
            reviewers_command(
                supervisor.dispatch,
                args(
                    reviewers_command="remove",
                    ticket=ticket.id,
                    slot="developer",
                    restart=False,
                ),
            )
        )
    with pytest.raises(ValueError, match="Unknown harness"):
        asyncio.run(
            reviewers_command(
                supervisor.dispatch,
                args(
                    reviewers_command="add",
                    ticket=ticket.id,
                    harness="gpt",
                    restart=False,
                ),
            )
        )
    assert json.dumps(supervisor.snapshot()["tickets"], sort_keys=True) == before


def test_play_and_stop_print_the_resulting_execution_state(
    tmp_path, monkeypatch, capsys
):
    import kanbeat.supervisor as module

    monkeypatch.setattr(module, "available", lambda _name: True)
    supervisor = Supervisor(tmp_path)
    asyncio.run(board_control_command(supervisor.dispatch, "play"))
    assert capsys.readouterr().out == "execution: running\n"
    asyncio.run(board_control_command(supervisor.dispatch, "stop"))
    assert capsys.readouterr().out == "execution: stopped\n"


def test_the_previous_instructions_are_upgraded_on_an_existing_board(tmp_path):
    assert PREVIOUS_INSTRUCTIONS_SHA256 in LEGACY_INSTRUCTION_HASHES
    assert "kanbeat assign" in INSTRUCTION
    assert "goes straight to Done" in INSTRUCTION
    board = Supervisor(tmp_path).board
    assert "kanbeat assign" in (board.root / "INSTRUCTIONS.md").read_text(
        encoding="utf-8"
    )


def test_the_slot_rule_is_shared_and_names_the_blocking_column():
    for column in ("backlog", "todo", "done", "failed", "archive"):
        assert not assignment_change_blocked(column, "running")
        assert not assignment_change_blocked(column, "stopped")
    for column in ("progress", "review"):
        assert assignment_change_blocked(column, "running")
        assert not assignment_change_blocked(column, "stopped")
        refusal = assignment_change_refusal("004", column)
        assert f"Ticket 004 is in {column.title()}" in refusal
        assert "kanbeat stop" in refusal
        assert "kanbeat play" in refusal


@pytest.mark.parametrize(
    "column", ["backlog", "todo", "progress", "review", "done", "failed", "archive"]
)
@pytest.mark.parametrize("execution", ["stopped", "running"])
def test_assign_follows_the_column_rule(tmp_path, column, execution):
    supervisor = Supervisor(tmp_path)
    ticket = ticket_in_column(supervisor, column)
    supervisor.runtime.data["execution"] = execution
    operations = []

    async def send(request):
        operations.append(request["op"])
        return await supervisor.dispatch(request)

    values = args(
        tickets=[ticket.id],
        slot="developer",
        harness="codex",
        cp=None,
        cp_file=None,
        restart=False,
    )
    if execution == "running" and column in ("progress", "review"):
        with pytest.raises(ValueError) as info:
            asyncio.run(assign_command(send, values))
        message = str(info.value)
        assert f"Ticket {ticket.id} is in {column.title()}" in message
        assert "kanbeat stop" in message
        assert "kanbeat play" in message
        assert operations == ["snapshot"]
        assert not supervisor.ticket_state(ticket.id)["developer"]
    else:
        asyncio.run(assign_command(send, values))
        assert operations == ["snapshot", "set_assignment"]
        assert supervisor.ticket_state(ticket.id)["developer"] == {"harness": "codex"}
    assert supervisor.runtime.data["execution"] == execution


@pytest.mark.parametrize("column", ["backlog", "todo", "done", "failed", "archive"])
def test_a_running_board_changes_prompt_and_reviewer_outside_active_columns(
    tmp_path, column
):
    supervisor = Supervisor(tmp_path)
    ticket = ticket_in_column(supervisor, column)
    supervisor.runtime.data["execution"] = "running"
    operations = []

    async def send(request):
        operations.append(request["op"])
        return await supervisor.dispatch(request)

    asyncio.run(
        assign_command(
            send,
            args(
                tickets=[ticket.id],
                slot="developer",
                harness="pi",
                cp="Be precise",
                cp_file=None,
                restart=False,
            ),
        )
    )
    asyncio.run(
        assign_command(
            send,
            args(
                tickets=[ticket.id],
                slot="reviewer-1",
                harness="claude",
                cp="Review carefully",
                cp_file=None,
                restart=False,
            ),
        )
    )
    state = supervisor.ticket_state(ticket.id)
    assert state["developer"] == {"harness": "pi", "cp": "Be precise"}
    assert state["reviewers"] == [
        {"id": "reviewer-1", "harness": "claude", "cp": "Review carefully"}
    ]
    assert operations == [
        "snapshot",
        "set_assignment",
        "snapshot",
        "set_assignment",
    ]
    assert supervisor.runtime.data["execution"] == "running"


@pytest.mark.parametrize("column", ["progress", "review"])
def test_remove_reviewer_refuses_active_columns_with_the_column_message(
    tmp_path, column
):
    supervisor = Supervisor(tmp_path)
    ticket = supervisor.board.create("Locked", column=column)
    state = supervisor.ticket_state(ticket.id)
    state["reviewers"] = [{"id": "reviewer-1", "harness": "codex"}]
    supervisor.save()
    supervisor.runtime.data["execution"] = "running"
    operations = []

    async def send(request):
        operations.append(request["op"])
        return await supervisor.dispatch(request)

    with pytest.raises(ValueError) as info:
        asyncio.run(
            reviewers_command(
                send,
                args(
                    reviewers_command="remove",
                    ticket=ticket.id,
                    slot="reviewer-1",
                    restart=False,
                ),
            )
        )
    message = str(info.value)
    assert f"Ticket {ticket.id} is in {column.title()}" in message
    assert "kanbeat stop" in message
    assert "kanbeat play" in message
    assert operations == ["snapshot"]
    assert [row["id"] for row in state["reviewers"]] == ["reviewer-1"]


def test_adding_and_removing_a_reviewer_in_todo_while_another_ticket_is_developed(
    tmp_path,
):
    supervisor = Supervisor(tmp_path)
    working = supervisor.board.create("Working", column="progress")
    waiting = supervisor.board.create("Waiting", column="todo")
    supervisor.ticket_state(working.id)["runtime"] = {
        "developer": {"status": "running", "activity": "working"}
    }
    supervisor.runtime.data["execution"] = "running"
    supervisor.save()
    operations = []

    async def send(request):
        operations.append(request["op"])
        return await supervisor.dispatch(request)

    asyncio.run(
        reviewers_command(
            send,
            args(
                reviewers_command="add",
                ticket=waiting.id,
                harness="codex",
                restart=False,
            ),
        )
    )
    assert operations == ["snapshot", "set_assignment"]
    assert supervisor.ticket_state(waiting.id)["reviewers"] == [
        {"id": "reviewer-1", "harness": "codex"}
    ]

    asyncio.run(
        reviewers_command(
            send,
            args(
                reviewers_command="remove",
                ticket=waiting.id,
                slot="reviewer-1",
                restart=False,
            ),
        )
    )
    assert operations == [
        "snapshot",
        "set_assignment",
        "snapshot",
        "remove_reviewer",
    ]
    assert supervisor.ticket_state(waiting.id)["reviewers"] == []
    assert supervisor.ticket_state(working.id)["runtime"] == {
        "developer": {"status": "running", "activity": "working"}
    }
    assert supervisor.runtime.data["execution"] == "running"


def test_a_running_slot_terminal_still_refuses_a_todo_change(tmp_path):
    supervisor = Supervisor(tmp_path)
    ticket = supervisor.board.create("Todo", column="todo")
    supervisor.runtime.data["execution"] = "running"
    supervisor.terminals.entries["terminal"] = {
        "ticket": ticket.id,
        "slot": "developer",
        "status": "running",
        "owner": "custom",
    }
    with pytest.raises(ValueError, match="interactive slot terminal"):
        asyncio.run(
            assign_command(
                supervisor.dispatch,
                args(
                    tickets=[ticket.id],
                    slot="developer",
                    harness="codex",
                    cp=None,
                    cp_file=None,
                    restart=False,
                ),
            )
        )
    assert not supervisor.ticket_state(ticket.id)["developer"]


def test_a_ticket_changed_in_todo_starts_with_the_new_settings(tmp_path):
    supervisor = Supervisor(tmp_path)
    ticket = supervisor.board.create("Todo", column="todo")
    supervisor.runtime.data["execution"] = "running"
    asyncio.run(
        assign_command(
            supervisor.dispatch,
            args(
                tickets=[ticket.id],
                slot="developer",
                harness="codex",
                cp=None,
                cp_file=None,
                restart=False,
            ),
        )
    )
    asyncio.run(
        assign_command(
            supervisor.dispatch,
            args(
                tickets=[ticket.id],
                slot="reviewer-1",
                harness="claude",
                cp=None,
                cp_file=None,
                restart=False,
            ),
        )
    )
    started = supervisor.board.move(ticket.id, "progress")
    state = supervisor.ticket_state(ticket.id)
    assert state["developer"] == {"harness": "codex"}
    assert state["reviewers"] == [{"id": "reviewer-1", "harness": "claude"}]
    assert supervisor.workers(started) == [("developer", state["developer"])]


def test_the_instructions_describe_where_slot_changes_are_allowed():
    assert SLOT_RULE_INSTRUCTIONS_SHA256 in LEGACY_INSTRUCTION_HASHES
    assert "Slot changes are allowed while the board runs" in INSTRUCTION
    assert "`progress/` or `review/`" in INSTRUCTION
    assert "kanbeat stop" in INSTRUCTION
    assert "kanbeat play" in INSTRUCTION

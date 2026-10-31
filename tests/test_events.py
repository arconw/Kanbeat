import asyncio

import pytest

from kanbeat.events import EVENT_LIMIT, EventLog, diff_events, format_event
from kanbeat.supervisor import Supervisor


def run(supervisor, **request):
    return asyncio.run(supervisor.dispatch(request))


def tick(supervisor):
    asyncio.run(supervisor.reconcile())


@pytest.fixture
def supervisor(tmp_path):
    instance = Supervisor(tmp_path)
    tick(instance)
    return instance


def test_the_first_look_at_the_board_is_a_baseline_without_events(tmp_path):
    instance = Supervisor(tmp_path)
    instance.board.create("Existing")
    tick(instance)
    assert instance.events.events == [] and instance.events.last == 0


def test_every_change_to_the_files_becomes_a_numbered_event(supervisor):
    first = supervisor.board.create("First task")
    tick(supervisor)
    supervisor.board.move(first.id, "todo")
    tick(supervisor)
    supervisor.board.find(first.id).path.unlink()
    tick(supervisor)
    supervisor.runtime.data["execution"] = "running"
    tick(supervisor)
    events = supervisor.events.events
    assert [(e["seq"], e["event"]) for e in events] == [
        (1, "created"),
        (2, "moved"),
        (3, "deleted"),
        (4, "execution"),
    ]
    assert events[1]["from"] == "backlog" and events[1]["to"] == "todo"
    assert events[0]["time"].endswith("Z")
    assert [format_event(e) for e in events] == [
        f"created #{first.id} in backlog  First task",
        f"moved #{first.id} backlog -> todo  First task",
        f"deleted #{first.id}  First task",
        "execution stopped -> running",
    ]


def test_changes_made_by_hand_and_by_kanbeat_look_the_same(supervisor):
    ticket = supervisor.board.create("Task")
    tick(supervisor)
    supervisor.move_ticket(ticket.id, "todo")
    tick(supervisor)
    ticket_path = supervisor.board.find(ticket.id).path
    ticket_path.rename(ticket_path.parents[1] / "review" / ticket_path.name)
    tick(supervisor)
    assert [(e["event"], e.get("to")) for e in supervisor.events.events] == [
        ("created", None),
        ("moved", "todo"),
        ("moved", "review"),
    ]


def test_an_unreadable_board_produces_no_events(supervisor):
    ticket = supervisor.board.create("Task")
    tick(supervisor)
    (supervisor.board.root / "todo" / "not-a-ticket.md").write_text("x")
    supervisor.board.find(ticket.id).path.unlink()
    tick(supervisor)
    assert [e["event"] for e in supervisor.events.events] == ["created"]
    (supervisor.board.root / "todo" / "not-a-ticket.md").unlink()
    tick(supervisor)
    assert [e["event"] for e in supervisor.events.events] == ["created", "deleted"]


def test_the_log_keeps_the_latest_events_and_survives_a_restart(tmp_path):
    first = Supervisor(tmp_path)
    tick(first)
    for number in range(EVENT_LIMIT + 20):
        first.runtime.data["execution"] = "running" if number % 2 == 0 else "stopped"
        tick(first)
    assert len(first.events.events) == EVENT_LIMIT
    last = first.events.last
    first.save()
    again = Supervisor(tmp_path)
    assert again.events.last == last
    assert again.events.since(last - 3) == again.events.events[-3:]


def test_the_events_operation_pages_forward_from_a_cursor(supervisor):
    for title in ("A", "B", "C"):
        supervisor.board.create(title)
        tick(supervisor)
    assert run(supervisor, op="events", since=None) == {"events": [], "last": 3}
    reply = run(supervisor, op="events", since=0, limit=2)
    assert [e["seq"] for e in reply["events"]] == [1, 2] and reply["last"] == 2
    reply = run(supervisor, op="events", since=2)
    assert [e["seq"] for e in reply["events"]] == [3] and reply["last"] == 3
    assert run(supervisor, op="events", since=3) == {"events": [], "last": 3}
    assert run(supervisor, op="events", since=40) == {"events": [], "last": 3}
    with pytest.raises(ValueError, match="limit must be between"):
        run(supervisor, op="events", since=0, limit=0)
    with pytest.raises(ValueError, match="since must be a whole number"):
        run(supervisor, op="events", since="x")
    assert run(supervisor, op="snapshot")["event_seq"] == 3


def test_events_need_no_running_board(supervisor):
    assert supervisor.runtime.data["execution"] == "stopped"
    supervisor.board.create("Task")
    tick(supervisor)
    assert supervisor.events.last == 1


def test_old_hook_state_is_dropped_on_start(tmp_path):
    first = Supervisor(tmp_path)
    first.runtime.data["hooks"] = {"1": {"status": "pending"}}
    first.runtime.data["hook_seq"] = 4
    first.save()
    again = Supervisor(tmp_path)
    assert "hooks" not in again.runtime.data and "hook_seq" not in again.runtime.data


def test_diff_events_orders_tickets_then_execution():
    before = {
        "tickets": {"001": ["todo", "A"], "002": ["done", "B"]},
        "execution": "stopped",
    }
    after = {
        "tickets": {"001": ["progress", "A"], "003": ["backlog", "C"]},
        "execution": "running",
    }
    assert [e["event"] for e in diff_events(before, after)] == [
        "moved",
        "deleted",
        "created",
        "execution",
    ]
    assert diff_events(before, before) == []
    assert EventLog is not None

import asyncio
from pathlib import Path

from kanbeat.filesystem import Board
from kanbeat.state import LocalState
from kanbeat.supervisor import Supervisor


def test_missing_state_is_stopped(tmp_path: Path):
    board = Board(tmp_path)
    board.ensure()
    board.create("Todo", column="todo")
    state = LocalState(board)
    assert state.data["execution"] == "stopped"
    assert not state.data["tickets"]


def test_supervisor_does_not_edit_ticket_file(tmp_path: Path):
    supervisor = Supervisor(tmp_path)
    ticket = supervisor.board.create("Ticket", "Body", column="review")
    old = ticket.path.read_bytes()
    config = supervisor.ticket_state(ticket.id)
    config["reviewers"] = [{"id": "reviewer-1", "harness": "claude"}]
    supervisor.save()
    assert ticket.path.read_bytes() == old


def test_reviewer_context_does_not_include_developer_output(tmp_path: Path):
    supervisor = Supervisor(tmp_path)
    ticket = supervisor.board.create("Implement", "Original description", column="review")
    state = supervisor.ticket_state(ticket.id)
    state["developer"]["session"] = "developer-session-secret"
    state["reviewers"] = [
        {"id": "reviewer-1", "harness": "codex"},
        {"id": "reviewer-2", "harness": "claude"},
    ]
    prompt = supervisor.prompt(ticket, "reviewer-1", False)
    assert "Original description" in prompt
    assert "developer-session-secret" not in prompt
    assert "TASKBEAT_VERDICT" in prompt


def test_archived_ticket_not_dispatched(tmp_path: Path):
    supervisor = Supervisor(tmp_path)
    ticket = supervisor.board.create("Archived", column="todo")
    supervisor.board.move(ticket.id, "archive")
    snapshot = supervisor.snapshot()
    assert snapshot["tickets"][0]["column"] == "archive"

"""Supervisor lifecycle regression cases."""

import asyncio
from pathlib import Path

import pytest

from kanbeat.filesystem import Board
from kanbeat.native import Outcome
from kanbeat.state import LocalState
from kanbeat.supervisor import Supervisor


@pytest.mark.asyncio
async def test_failed_native_stop_awaits_other_workers_and_preserves_blocked_job(
    tmp_path,
):
    supervisor = Supervisor(tmp_path)
    ticket = supervisor.board.create("Blocked native stop")
    key = supervisor.slot_key(ticket.id, "developer")
    supervisor.ticket_state(ticket.id)["runtime"] = {"developer": {"status": "running"}}

    class CannotStop:
        async def stop(self):
            raise RuntimeError("child cannot exit")

    finished = asyncio.Event()

    async def clean_job():
        await asyncio.sleep(0.01)
        finished.set()

    async def blocked_job():
        await asyncio.Event().wait()

    pending = asyncio.create_task(blocked_job())
    clean = asyncio.create_task(clean_job())
    supervisor.runs[key] = CannotStop()
    supervisor.jobs[key] = pending
    supervisor.jobs["other:worker"] = clean
    try:
        with pytest.raises(RuntimeError, match="Native cleanup incomplete"):
            await supervisor.stop()
        assert finished.is_set()
        assert clean.done()
        assert key in supervisor.jobs
        assert "other:worker" not in supervisor.jobs
        assert supervisor.ticket_state(ticket.id)["runtime"]["developer"]["status"] == (
            "recovery_blocked"
        )
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


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
    original = ticket.path.read_bytes()
    state = supervisor.ticket_state(ticket.id)
    state["reviewers"] = [{"id": "reviewer-1", "harness": "claude"}]
    supervisor.save()
    assert ticket.path.read_bytes() == original


def test_reviewer_prompt_uses_path_not_body_or_developer_logs(tmp_path: Path):
    supervisor = Supervisor(tmp_path)
    ticket = supervisor.board.create(
        "Implement", "Confidential ticket detail", column="review"
    )
    state = supervisor.ticket_state(ticket.id)
    state["developer"]["session"] = "developer-secret"
    state["reviewers"] = [
        {"id": "reviewer-1", "harness": "codex"},
        {"id": "reviewer-2", "harness": "claude"},
    ]
    prompt = supervisor.prompt(ticket, "reviewer-1", False)
    assert ".kanbeat/review/" in prompt
    assert "Confidential ticket detail" not in prompt
    assert "developer-secret" not in prompt
    assert "KANBEAT_VERDICT" in prompt


def test_archived_ticket_not_dispatched(tmp_path: Path):
    supervisor = Supervisor(tmp_path)
    ticket = supervisor.board.create("Archived", column="todo")
    supervisor.board.move(ticket.id, "archive")
    assert supervisor.snapshot()["tickets"][0]["column"] == "archive"


@pytest.mark.asyncio
async def test_modified_ticket_is_redispatched_to_same_session(
    tmp_path: Path, monkeypatch
):
    import kanbeat.supervisor as module

    supervisor = Supervisor(tmp_path)
    ticket = supervisor.board.create("Build", "Before change", column="progress")
    state = supervisor.ticket_state(ticket.id)
    state["phase"] = "progress"
    state["iteration"] = 1
    supervisor.runtime.data["execution"] = "running"
    captured = []

    class FakeRun:
        stopping = False

        def __init__(
            self, harness, prompt, session, cwd, log, on_session=None, on_process=None
        ):
            captured.append((prompt, session))
            self.on_session = on_session

        async def execute(self):
            self.on_session("native-session-1")
            if len(captured) == 1:
                ticket.path.write_text("# Build\n\nAfter change\n", encoding="utf-8")
            return Outcome(True, "completed", "native-session-1", "done")

        async def stop(self):
            self.stopping = True

    monkeypatch.setattr(module, "NativeRun", FakeRun)
    monkeypatch.setattr(module, "available", lambda _name: True)
    await supervisor.start_worker(ticket, "developer", state["developer"])
    await supervisor.jobs["001:developer"]
    assert len(captured) == 2
    assert captured[1][1] == "native-session-1"
    assert "has changed" in captured[1][0]
    assert "After change" not in captured[1][0]
    assert state["runtime"]["developer"]["status"] == "completed"
    assert state["runtime"]["developer"]["task_hash"] == supervisor.content_hash(
        supervisor.board.find(ticket.id)
    )


@pytest.mark.asyncio
async def test_finished_reviewer_verdict_invalidated_when_ticket_changes(
    tmp_path: Path, monkeypatch
):
    import kanbeat.supervisor as module

    supervisor = Supervisor(tmp_path)
    ticket = supervisor.board.create("Review", "Version one", column="review")
    state = supervisor.ticket_state(ticket.id)
    state["phase"] = "review"
    state["iteration"] = 2
    state["reviewers"] = [{"id": "reviewer-1", "harness": "codex"}]
    old_hash = supervisor.content_hash(ticket)
    state["review_results"] = {
        "reviewer-1": {
            "verdict": "approve",
            "iteration": 2,
            "task_hash": old_hash,
        }
    }
    state["runtime"]["reviewer-1"] = {
        "stage": "review",
        "iteration": 2,
        "status": "completed",
        "task_hash": old_hash,
    }
    supervisor.runtime.data["execution"] = "running"
    ticket.path.write_text("# Review\n\nVersion two\n", encoding="utf-8")
    observed = []

    async def fake_start(ticket, worker, assignment):
        observed.append((ticket.id, worker))

    monkeypatch.setattr(supervisor, "start_worker", fake_start)
    monkeypatch.setattr(module, "available", lambda _name: True)
    await supervisor.reconcile()
    assert not state["review_results"]
    assert state["runtime"]["reviewer-1"]["status"] == "stale"
    assert observed == [("001", "reviewer-1")]
    assert supervisor.board.find("001").column == "review"


@pytest.mark.asyncio
async def test_external_move_interrupts_old_worker(tmp_path: Path, monkeypatch):
    import kanbeat.supervisor as module

    supervisor = Supervisor(tmp_path)
    ticket = supervisor.board.create("Implement", "Details", column="progress")
    state = supervisor.ticket_state(ticket.id)
    state["phase"] = "progress"
    state["iteration"] = 1
    supervisor.runtime.data["execution"] = "running"
    interrupted = asyncio.Event()
    stopped = []

    class FakeRun:
        stopping = False

        def __init__(
            self, harness, prompt, session, cwd, log, on_session=None, on_process=None
        ):
            pass

        async def execute(self):
            await interrupted.wait()
            return Outcome(False, "stopped", None, "")

        async def stop(self):
            self.stopping = True
            stopped.append(True)
            interrupted.set()

    monkeypatch.setattr(module, "NativeRun", FakeRun)
    monkeypatch.setattr(module, "available", lambda _name: True)
    await supervisor.start_worker(ticket, "developer", state["developer"])
    await asyncio.sleep(0)
    supervisor.board.move(ticket.id, "done")
    await supervisor.reconcile()
    assert stopped
    assert supervisor.board.find(ticket.id).column == "done"
    assert not any(not job.done() for job in supervisor.jobs.values())


@pytest.mark.asyncio
async def test_reviewer_failure_stops_parallel_workers(tmp_path: Path, monkeypatch):
    import kanbeat.supervisor as module

    supervisor = Supervisor(tmp_path)
    ticket = supervisor.board.create("Review", "Please review", column="review")
    state = supervisor.ticket_state(ticket.id)
    state["phase"] = "review"
    state["iteration"] = 1
    state["reviewers"] = [{"id": "reviewer-1"}, {"id": "reviewer-2"}]
    supervisor.runtime.data["execution"] = "running"
    gate = asyncio.Event()
    stopped = []

    class FakeRun:
        stopping = False

        def __init__(
            self, harness, prompt, session, cwd, log, on_session=None, on_process=None
        ):
            pass

        async def execute(self):
            await gate.wait()
            return Outcome(False, "interrupted", None, "")

        async def stop(self):
            self.stopping = True
            stopped.append(True)
            gate.set()

    monkeypatch.setattr(module, "NativeRun", FakeRun)
    monkeypatch.setattr(module, "available", lambda _name: True)
    await supervisor.start_worker(ticket, "reviewer-1", state["reviewers"][0])
    await supervisor.start_worker(ticket, "reviewer-2", state["reviewers"][1])
    await asyncio.sleep(0)
    await supervisor.fail_ticket(ticket.id, "review", 1, "reviewer failed")
    assert supervisor.board.find(ticket.id).column == "failed"
    assert len(stopped) == 2


@pytest.mark.asyncio
async def test_crash_recovery_keeps_session_but_stops_dispatch(tmp_path: Path):
    supervisor = Supervisor(tmp_path)
    ticket = supervisor.board.create("Resume", column="progress")
    state = supervisor.ticket_state(ticket.id)
    state["developer"]["session"] = "native-session-id"
    state["runtime"]["developer"] = {
        "stage": "progress",
        "iteration": 1,
        "status": "running",
    }
    supervisor.runtime.data["execution"] = "running"
    await supervisor.recover_orphans()
    assert supervisor.runtime.data["execution"] == "stopped"
    assert state["developer"]["session"] == "native-session-id"
    assert state["runtime"]["developer"]["status"] == "interrupted"


@pytest.mark.asyncio
async def test_play_after_orphan_was_resolved(tmp_path: Path, monkeypatch):
    import kanbeat.supervisor as module

    supervisor = Supervisor(tmp_path)
    ticket = supervisor.board.create("Safe retry", column="progress")
    state = supervisor.ticket_state(ticket.id)
    state["runtime"]["developer"] = {
        "status": "recovery_blocked",
        "pid": 123456,
        "process_identity": "linux:111",
    }
    monkeypatch.setattr(module, "process_identity", lambda pid: None)
    monkeypatch.setattr(module, "available", lambda name: True)
    await supervisor.play()
    assert supervisor.runtime.data["execution"] == "running"
    assert state["runtime"]["developer"]["status"] == "interrupted"

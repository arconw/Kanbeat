"""Supervisor lifecycle regression cases."""

import asyncio
from pathlib import Path

import pytest

from kanbeat.filesystem import Board
from kanbeat.native import Outcome
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
    supervisor.runtime.config["default_harness"] = "pi"
    ticket = supervisor.board.create("Build", "Before change", column="progress")
    state = supervisor.ticket_state(ticket.id)
    state["phase"] = "progress"
    state["iteration"] = 1
    supervisor.runtime.data["execution"] = "running"
    captured = []

    class FakeRun:
        stopping = False

        def __init__(
            self,
            harness,
            prompt,
            session,
            cwd,
            log,
            on_session=None,
            on_process=None,
            on_activity=None,
            command_builder=None,
            env=None,
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
async def test_worker_activity_is_persisted_and_cleared(tmp_path: Path, monkeypatch):
    import kanbeat.supervisor as module

    supervisor = Supervisor(tmp_path)
    supervisor.runtime.config["default_harness"] = "pi"
    ticket = supervisor.board.create("Activity", column="progress")
    state = supervisor.ticket_state(ticket.id)
    state["phase"] = "progress"
    state["iteration"] = 1
    supervisor.runtime.data["execution"] = "running"
    seen = []

    class FakeRun:
        stopping = False

        def __init__(
            self,
            harness,
            prompt,
            session,
            cwd,
            log,
            on_session=None,
            on_process=None,
            on_activity=None,
            command_builder=None,
            env=None,
        ):
            self.on_activity = on_activity

        async def execute(self):
            self.on_activity("working")
            seen.append(state["runtime"]["developer"].get("activity"))
            self.on_activity("waiting")
            seen.append(state["runtime"]["developer"].get("activity"))
            return Outcome(True, "completed", None, "done")

        async def stop(self):
            self.stopping = True

    monkeypatch.setattr(module, "NativeRun", FakeRun)
    monkeypatch.setattr(module, "available", lambda _name: True)
    await supervisor.start_worker(ticket, "developer", state["developer"])
    await supervisor.jobs["001:developer"]
    assert seen == ["working", "waiting"]
    assert "activity" not in state["runtime"]["developer"]


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
    supervisor.runtime.config["default_harness"] = "pi"
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
            self,
            harness,
            prompt,
            session,
            cwd,
            log,
            on_session=None,
            on_process=None,
            on_activity=None,
            command_builder=None,
            env=None,
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
    supervisor.runtime.config["default_harness"] = "pi"
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
            self,
            harness,
            prompt,
            session,
            cwd,
            log,
            on_session=None,
            on_process=None,
            on_activity=None,
            command_builder=None,
            env=None,
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


@pytest.mark.asyncio
async def test_chain_is_released_one_ticket_at_a_time_and_independent_tickets_run_together(
    tmp_path: Path, monkeypatch
):
    import kanbeat.supervisor as module

    monkeypatch.setattr(module, "available", lambda _name: True)
    supervisor = Supervisor(tmp_path)
    supervisor.runtime.config["default_harness"] = "pi"
    supervisor.runtime.config["max_parallel"] = 5
    supervisor.runtime.data["execution"] = "running"
    first = supervisor.board.create("First", column="todo")
    second = supervisor.board.create(
        "Second", f"# Second\n\nBlocked by: #{first.id}\n", column="todo"
    )
    third = supervisor.board.create(
        "Third", f"# Third\n\nBlocked by: #{second.id}\n", column="todo"
    )
    fourth = supervisor.board.create(
        "Fourth", f"# Fourth\n\nBlocked by: #{third.id}\n", column="todo"
    )
    loose = supervisor.board.create("Loose", column="todo")
    started = []

    async def start(ticket, worker, assignment):
        started.append(ticket.id)

    monkeypatch.setattr(supervisor, "start_worker", start)
    for _ in range(6):
        await supervisor.reconcile()
    columns = {t.id: t.column for t in supervisor.board.scan()[0]}
    assert columns == {
        first.id: "progress",
        second.id: "todo",
        third.id: "todo",
        fourth.id: "todo",
        loose.id: "progress",
    }
    assert sorted(set(started)) == [first.id, loose.id]
    supervisor.board.move(first.id, "done")
    for _ in range(3):
        await supervisor.reconcile()
    columns = {t.id: t.column for t in supervisor.board.scan()[0]}
    assert columns[second.id] == "progress"
    assert columns[third.id] == columns[fourth.id] == "todo"


@pytest.mark.asyncio
async def test_parallel_limit_caps_active_tickets(tmp_path: Path, monkeypatch):
    import kanbeat.supervisor as module

    monkeypatch.setattr(module, "available", lambda _name: True)
    supervisor = Supervisor(tmp_path)
    supervisor.runtime.config["default_harness"] = "pi"
    supervisor.runtime.config["max_parallel"] = 2
    supervisor.runtime.data["execution"] = "running"
    for number in range(4):
        supervisor.board.create(f"T{number}", column="todo")

    async def start(ticket, worker, assignment):
        return None

    monkeypatch.setattr(supervisor, "start_worker", start)
    for _ in range(6):
        await supervisor.reconcile()
    columns = [t.column for t in supervisor.board.scan()[0]]
    assert columns.count("progress") == 2
    assert columns.count("todo") == 2


@pytest.mark.asyncio
async def test_blocked_ticket_cannot_be_moved_to_progress_by_hand(tmp_path: Path):
    supervisor = Supervisor(tmp_path)
    blocker = supervisor.board.create("Blocker", column="todo")
    waiting = supervisor.board.create(
        "Waiting", f"# Waiting\n\nBlocked by: #{blocker.id}\n", column="todo"
    )
    with pytest.raises(
        ValueError, match="Blocked by unfinished tickets: #" + blocker.id
    ):
        await supervisor.dispatch(
            {"op": "move", "ticket": waiting.id, "column": "progress"}
        )
    snapshot = supervisor.snapshot()
    rows = {row["id"]: row for row in snapshot["tickets"]}
    assert rows[waiting.id]["blocked"] is True
    assert [row["id"] for row in rows[blocker.id]["blocks"]] == [waiting.id]


@pytest.mark.asyncio
async def test_todo_ticket_waits_until_blockers_are_done_or_archived(
    tmp_path: Path, monkeypatch
):
    import kanbeat.supervisor as module

    monkeypatch.setattr(module, "available", lambda _name: True)
    supervisor = Supervisor(tmp_path)
    supervisor.runtime.config["default_harness"] = "pi"
    supervisor.runtime.data["execution"] = "running"
    blockers = {
        column: supervisor.board.create(column.title(), column=column)
        for column in ("backlog", "failed", "progress")
    }
    waiting = [
        supervisor.board.create(
            f"Waits {column}", f"# Waits\n\nBlocked by: #{ticket.id}\n", column="todo"
        )
        for column, ticket in blockers.items()
    ]
    started = []

    async def start(ticket, worker, assignment):
        started.append(ticket.id)

    monkeypatch.setattr(supervisor, "start_worker", start)
    for _ in range(4):
        await supervisor.reconcile()
    assert {supervisor.board.find(t.id).column for t in waiting} == {"todo"}
    supervisor.board.move(blockers["backlog"].id, "done")
    supervisor.board.move(blockers["failed"].id, "done")
    supervisor.board.move(blockers["failed"].id, "archive")
    for _ in range(4):
        await supervisor.reconcile()
    columns = {t.id: supervisor.board.find(t.id).column for t in waiting}
    assert columns[waiting[0].id] == "progress"
    assert columns[waiting[1].id] == "progress"
    assert columns[waiting[2].id] == "todo"


@pytest.mark.asyncio
async def test_stuck_tickets_are_named_with_the_reason_and_reported_again_on_change(
    tmp_path: Path, monkeypatch
):
    import kanbeat.supervisor as module

    monkeypatch.setattr(module, "available", lambda _name: True)
    supervisor = Supervisor(tmp_path)
    supervisor.runtime.config["default_harness"] = "pi"
    supervisor.runtime.data["execution"] = "running"
    parked = supervisor.board.create("Parked", column="backlog")
    broken = supervisor.board.create("Broken", column="failed")
    missing = supervisor.board.create(
        "Typo", "# Typo\n\nBlocked by: #099\n", column="todo"
    )
    on_parked = supervisor.board.create(
        "Parked dep", f"# Parked dep\n\nBlocked by: #{parked.id}\n", column="todo"
    )
    on_broken = supervisor.board.create(
        "Broken dep", f"# Broken dep\n\nBlocked by: #{broken.id}\n", column="todo"
    )
    first = supervisor.board.create("Loop A", column="todo")
    second = supervisor.board.create(
        "Loop B", f"# Loop B\n\nBlocked by: #{first.id}\n", column="todo"
    )
    first.path.write_text(f"# Loop A\n\nBlocked by: #{second.id}\n", encoding="utf-8")

    async def start(ticket, worker, assignment):
        raise AssertionError("no worker may start")

    monkeypatch.setattr(supervisor, "start_worker", start)

    def warnings():
        return [
            row["message"]
            for row in supervisor.runtime.data["notices"]
            if row["level"] == "warning"
        ]

    for _ in range(3):
        await supervisor.reconcile()
    assert len(warnings()) == 1
    text = warnings()[0]
    assert f"#{missing.id} (#099 is missing)" in text
    assert f"#{on_parked.id} (#{parked.id} is in backlog)" in text
    assert f"#{on_broken.id} (#{broken.id} is in failed)" in text
    assert f"#{first.id} (dependency cycle)" in text
    assert f"#{second.id} (dependency cycle)" in text

    supervisor.board.move(on_broken.id, "done")
    for _ in range(3):
        await supervisor.reconcile()
    assert len(warnings()) == 2
    assert f"#{on_broken.id}" not in warnings()[-1]
    assert f"#{on_parked.id} (#{parked.id} is in backlog)" in warnings()[-1]


@pytest.mark.asyncio
async def test_explicit_move_can_override_a_block(tmp_path: Path):
    supervisor = Supervisor(tmp_path)
    blocker = supervisor.board.create("Blocker", column="backlog")
    waiting = supervisor.board.create(
        "Waiting", f"# Waiting\n\nBlocked by: #{blocker.id}\n", column="todo"
    )
    with pytest.raises(ValueError, match="Blocked by unfinished"):
        await supervisor.dispatch(
            {"op": "move", "ticket": waiting.id, "column": "progress"}
        )
    supervisor.board.move(waiting.id, "archive")
    with pytest.raises(ValueError, match="Blocked by unfinished"):
        await supervisor.dispatch(
            {"op": "restore", "ticket": waiting.id, "column": "progress"}
        )
    assert supervisor.board.find(waiting.id).column == "archive"
    supervisor.board.move(waiting.id, "todo")
    await supervisor.dispatch(
        {"op": "move", "ticket": waiting.id, "column": "progress", "force": True}
    )
    assert supervisor.board.find(waiting.id).column == "progress"

import asyncio
from pathlib import Path

import pytest

from kanbeat.native import Outcome
from kanbeat.supervisor import Supervisor


@pytest.fixture
def supervisor(tmp_path: Path):
    instance = Supervisor(tmp_path)
    instance.runtime.save_config(
        {"transition_hook": {"harness": "claude", "prompt": "Sync issue"}}
    )
    return instance


def test_disabled_and_same_column_moves_do_not_queue(tmp_path):
    instance = Supervisor(tmp_path)
    ticket = instance.board.create("Task")
    instance.move_ticket(ticket.id, "todo")
    assert not instance.hooks.entries
    instance.runtime.save_config({"transition_hook": {"prompt": "Sync"}})
    instance.move_ticket(ticket.id, "todo")
    assert not instance.hooks.entries


@pytest.mark.asyncio
async def test_hook_runs_once_with_isolated_session_and_current_path(
    supervisor, monkeypatch
):
    import kanbeat.hooks as module

    ticket = supervisor.board.create("Task", "Private body")
    supervisor.ticket_state(ticket.id)["developer"]["session"] = "developer-session"
    supervisor.move_ticket(ticket.id, "todo")
    moved = supervisor.board.find(ticket.id)
    moved.path.rename(moved.path.with_name("20-001-renamed.md"))
    captured = []

    class FakeRun:
        stopping = False

        def __init__(self, harness, prompt, session, cwd, log, on_process):
            captured.append((harness, prompt, session))

        async def execute(self):
            return Outcome(True, "completed", "hook-session", "Done")

    monkeypatch.setattr(module, "NativeRun", FakeRun)
    monkeypatch.setattr(module, "available", lambda name: True)
    supervisor.runtime.data["execution"] = "running"
    await supervisor.reconcile()
    await supervisor.jobs["hook:1"]
    assert captured[0][0] == "claude"
    assert captured[0][2] is None
    assert ".kanbeat/todo/20-001-renamed.md" in captured[0][1]
    assert "Private body" not in captured[0][1]
    assert "developer-session" not in captured[0][1]
    assert supervisor.hooks.entries["1"]["status"] == "completed"
    assert supervisor.board.find(ticket.id).column == "todo"
    restored = Supervisor(supervisor.board.project)
    assert restored.hooks.entries["1"]["status"] == "completed"
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_missing_hook_harness_preserves_ticket(supervisor, monkeypatch):
    monkeypatch.setattr("kanbeat.hooks.available", lambda name: False)
    ticket = supervisor.board.create("Task")
    supervisor.move_ticket(ticket.id, "done")
    supervisor.runtime.data["execution"] = "running"
    await supervisor.hooks.execute("1", supervisor.runs)
    assert supervisor.hooks.entries["1"]["status"] == "failed"
    assert supervisor.board.find(ticket.id).column == "done"
    assert supervisor.runtime.data["notices"][-1]["level"] == "warning"


@pytest.mark.asyncio
async def test_stop_interrupts_hook_without_replay(supervisor, monkeypatch):
    import kanbeat.hooks as module

    started = asyncio.Event()
    stopped = asyncio.Event()

    class FakeRun:
        stopping = False

        def __init__(self, *args, **kwargs):
            pass

        async def execute(self):
            started.set()
            await stopped.wait()
            return Outcome(False, "Stopped", None, "")

        async def stop(self):
            self.stopping = True
            stopped.set()

    monkeypatch.setattr(module, "NativeRun", FakeRun)
    monkeypatch.setattr(module, "available", lambda name: True)
    ticket = supervisor.board.create("Task")
    supervisor.move_ticket(ticket.id, "todo")
    supervisor.runtime.data["execution"] = "running"
    await supervisor.reconcile()
    await started.wait()
    await supervisor.stop()
    assert supervisor.hooks.entries["1"]["status"] == "interrupted"
    assert supervisor.board.find(ticket.id).column == "todo"
    assert not supervisor.runs


@pytest.mark.asyncio
async def test_restart_does_not_replay_uncertain_hook(supervisor):
    ticket = supervisor.board.create("Task")
    supervisor.move_ticket(ticket.id, "todo")
    supervisor.hooks.entries["1"]["status"] = "running"
    supervisor.runtime.data["execution"] = "running"
    supervisor.save()
    restored = Supervisor(supervisor.board.project)
    await restored.recover_orphans()
    assert restored.hooks.entries["1"]["status"] == "interrupted"
    assert restored.runtime.data["execution"] == "stopped"


@pytest.mark.parametrize("value", [None, [], {"harness": "unknown"}, {"prompt": 5}])
def test_invalid_hook_configuration_is_not_saved(supervisor, value):
    with pytest.raises(ValueError):
        supervisor.runtime.save_config(
            {"global_cp": "Changed", "transition_hook": value}
        )
    assert supervisor.runtime.config["global_cp"] == ""


@pytest.mark.asyncio
async def test_full_queue_lifecycle_serializes_hooks(supervisor, monkeypatch):
    import kanbeat.hooks as hooks_module
    import kanbeat.supervisor as supervisor_module

    observed = []

    class FakeRun:
        stopping = False

        def __init__(self, harness, prompt, session, cwd, log, **kwargs):
            if log.name.startswith("hook-"):
                observed.append(prompt)

        async def execute(self):
            return Outcome(True, "completed", None, "Done")

    monkeypatch.setattr(hooks_module, "NativeRun", FakeRun)
    monkeypatch.setattr(supervisor_module, "NativeRun", FakeRun)
    monkeypatch.setattr(hooks_module, "available", lambda name: True)
    monkeypatch.setattr(supervisor_module, "available", lambda name: True)
    first = supervisor.board.create("First", column="todo")
    second = supervisor.board.create("Second", column="todo")
    await supervisor.play()
    for _ in range(40):
        await supervisor.reconcile()
        await asyncio.sleep(0)
    assert supervisor.board.find(first.id).column == "done"
    assert supervisor.board.find(second.id).column == "done"
    assert [
        (e["ticket"], e["from"], e["to"], e["status"])
        for e in supervisor.hooks.entries.values()
    ] == [
        (ticket.id, before, after, "completed")
        for ticket in (first, second)
        for before, after in (
            ("todo", "progress"),
            ("progress", "review"),
            ("review", "done"),
        )
    ]
    assert len(observed) == 6


@pytest.mark.asyncio
async def test_hook_task_scheduled_before_stop_does_not_launch(supervisor, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Stopped board must not launch hooks")

    monkeypatch.setattr("kanbeat.hooks.NativeRun", unexpected)
    ticket = supervisor.board.create("Task")
    supervisor.move_ticket(ticket.id, "todo")
    supervisor.runtime.data["execution"] = "running"
    await supervisor.reconcile()
    await supervisor.stop()
    assert supervisor.hooks.entries["1"]["status"] == "pending"


@pytest.mark.asyncio
async def test_hook_failure_does_not_retry_or_stop_board(supervisor, monkeypatch):
    class FailedRun:
        stopping = False

        def __init__(self, *args, **kwargs):
            pass

        async def execute(self):
            return Outcome(False, "Hook failed", None, "")

    monkeypatch.setattr("kanbeat.hooks.NativeRun", FailedRun)
    monkeypatch.setattr("kanbeat.hooks.available", lambda name: True)
    ticket = supervisor.board.create("Task")
    supervisor.move_ticket(ticket.id, "done")
    supervisor.runtime.data["execution"] = "running"
    await supervisor.hooks.execute("1", supervisor.runs)
    assert supervisor.hooks.entries["1"]["status"] == "failed"
    assert supervisor.board.find(ticket.id).column == "done"
    assert supervisor.runtime.data["execution"] == "running"
    await supervisor.hooks.execute("1", supervisor.runs)
    assert supervisor.hooks.entries["1"]["status"] == "failed"


@pytest.mark.asyncio
async def test_real_native_process_executes_hook(supervisor, monkeypatch):
    import sys

    script = (
        "import json; "
        "print(json.dumps({'type': 'system', 'session_id': 'hook-session'})); "
        "print(json.dumps({'type': 'result', 'subtype': 'success', 'result': 'Synced'}))"
    )
    monkeypatch.setattr(
        "kanbeat.native.command", lambda *args: ([sys.executable, "-c", script], None)
    )
    monkeypatch.setattr("kanbeat.hooks.available", lambda name: True)
    ticket = supervisor.board.create("Task")
    supervisor.move_ticket(ticket.id, "done")
    supervisor.runtime.data["execution"] = "running"
    await supervisor.hooks.execute("1", supervisor.runs)
    assert supervisor.hooks.entries["1"]["status"] == "completed"
    assert not supervisor.runs
    assert "Synced" in (supervisor.state_dir / "logs" / "hook-1.log").read_text()
    assert "pid" not in supervisor.hooks.entries["1"]


@pytest.mark.asyncio
async def test_real_native_process_is_terminated_on_stop(supervisor, monkeypatch):
    import sys

    from kanbeat.native import process_identity

    monkeypatch.setattr(
        "kanbeat.native.command",
        lambda *args: (
            [sys.executable, "-c", "import time; time.sleep(60)"],
            None,
        ),
    )
    monkeypatch.setattr("kanbeat.hooks.available", lambda name: True)
    ticket = supervisor.board.create("Task")
    supervisor.move_ticket(ticket.id, "done")
    supervisor.runtime.data["execution"] = "running"
    await supervisor.reconcile()
    for _ in range(100):
        pid = supervisor.hooks.entries["1"].get("pid")
        if pid is not None:
            break
        await asyncio.sleep(0.01)
    assert pid is not None
    await asyncio.wait_for(supervisor.stop(), timeout=10)
    assert process_identity(pid) is None
    assert supervisor.hooks.entries["1"]["status"] == "interrupted"


def test_invalid_hook_config_file_rejected_at_startup(tmp_path):
    instance = Supervisor(tmp_path)
    instance.runtime.config_path.write_text('{"transition_hook": {"prompt": 123}}')
    with pytest.raises(ValueError, match="prompt must be text"):
        Supervisor(tmp_path)

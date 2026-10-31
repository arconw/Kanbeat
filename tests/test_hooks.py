import asyncio
import os
import sys

import pytest
import pytest_asyncio

from kanbeat import hooks
from kanbeat.supervisor import Supervisor

SCRIPT = (
    "import sys; print('READY', flush=True); "
    "[print('ECHO:' + line.rstrip(), flush=True) for line in sys.stdin]"
)


@pytest_asyncio.fixture
async def supervisor(tmp_path, tmp_path_factory, monkeypatch):
    bin_dir = tmp_path_factory.mktemp("bin")
    fake = bin_dir / "claude"
    fake.write_text(f"#!{sys.executable}\nexec({SCRIPT!r})\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path_factory.mktemp("claude")))
    monkeypatch.setattr("kanbeat.session_groups.available", lambda name: True)
    monkeypatch.setattr("kanbeat.supervisor.available", lambda name: True)
    monkeypatch.setattr(hooks, "available", lambda name: True)
    monkeypatch.setattr(hooks, "WAKE_QUIET", 0.2)
    monkeypatch.setattr(hooks, "WAKE_GAP", 0.0)
    monkeypatch.setattr(hooks, "START_QUIET", 0.2)
    instance = Supervisor(tmp_path)
    instance.runtime.save_config(
        {
            "default_harness": "claude",
            "transition_hook": {"harness": "claude", "prompt": "Tell me about moves"},
        }
    )

    async def forbidden():
        raise AssertionError("these tests must not start a real codex or opencode host")

    instance.session_groups.codex_host = forbidden
    instance.session_groups.opencode_host = forbidden
    yield instance
    await instance.terminals.stop()


async def wake(supervisor):
    await supervisor.reconcile()
    await supervisor.subscriber.guarded_step()


async def on_screen(supervisor, terminal_id, text, seconds=6):
    for _ in range(int(seconds / 0.02)):
        session = supervisor.terminals.sessions.get(terminal_id)
        if session is not None and text in session.snapshot()["text"]:
            return
        await asyncio.sleep(0.02)
    shown = supervisor.terminals.sessions[terminal_id].snapshot()["text"]
    pytest.fail(f"{text!r} never appeared; the screen shows {shown!r}")


def hook_entries(supervisor):
    return [
        entry
        for entry in supervisor.terminals.entries.values()
        if entry.get("role") == "hook"
    ]


async def started(supervisor):
    await wake(supervisor)
    ticket = supervisor.board.create("First task")
    await wake(supervisor)
    [entry] = hook_entries(supervisor)
    await on_screen(supervisor, entry["id"], "READY")
    return ticket, entry


@pytest.mark.asyncio
async def test_the_hook_stays_quiet_until_the_board_changes(supervisor):
    await wake(supervisor)
    await wake(supervisor)
    assert hook_entries(supervisor) == []
    assert supervisor.runtime.data["hook"]["cursor"] == 0


@pytest.mark.asyncio
async def test_the_first_event_starts_one_ungrouped_session_with_the_instructions(
    supervisor,
):
    ticket, entry = await started(supervisor)
    assert (entry["title"], entry["group"], entry["owner"]) == (
        "Transition hook",
        "",
        "custom",
    )
    assert entry["harness"] == "claude"
    assert "Tell me about moves" in entry["prompt"]
    assert f"created #{ticket.id} in backlog" in entry["prompt"]
    assert ".kanbeat/INSTRUCTIONS.md" in entry["prompt"]
    assert supervisor.runtime.data["hook"]["cursor"] == 1
    await wake(supervisor)
    assert len(hook_entries(supervisor)) == 1


@pytest.mark.asyncio
async def test_a_quiet_running_session_is_woken_by_typing_the_new_events(supervisor):
    ticket, entry = await started(supervisor)
    await asyncio.sleep(0.4)
    supervisor.board.move(ticket.id, "todo")
    await wake(supervisor)
    await on_screen(supervisor, entry["id"], "ECHO:Kanbeat board events #2 to #2:")
    await on_screen(
        supervisor, entry["id"], f"ECHO:#2 moved #{ticket.id} backlog -> todo"
    )
    await on_screen(supervisor, entry["id"], "ECHO:Continue with your standing")
    assert supervisor.runtime.data["hook"]["cursor"] == 2


@pytest.mark.asyncio
async def test_a_busy_session_is_not_interrupted(supervisor, monkeypatch):
    ticket, entry = await started(supervisor)
    session = supervisor.terminals.sessions[entry["id"]]
    monkeypatch.setattr(session, "quiet_for", lambda: 0.0)
    supervisor.board.move(ticket.id, "todo")
    await wake(supervisor)
    assert supervisor.runtime.data["hook"]["cursor"] == 1
    assert "ECHO:Kanbeat board events" not in session.snapshot()["text"]
    monkeypatch.setattr(session, "quiet_for", lambda: 99.0)
    await wake(supervisor)
    await on_screen(supervisor, entry["id"], "ECHO:Kanbeat board events #2 to #2:")
    assert supervisor.runtime.data["hook"]["cursor"] == 2


@pytest.mark.asyncio
async def test_a_stopped_session_is_resumed_and_told(supervisor):
    ticket, entry = await started(supervisor)
    await supervisor.terminals.sessions[entry["id"]].stop()
    assert entry["status"] == "stopped"
    supervisor.board.move(ticket.id, "todo")
    await wake(supervisor)
    assert entry["status"] == "running"
    assert len(hook_entries(supervisor)) == 1
    await on_screen(supervisor, entry["id"], "ECHO:Kanbeat board events #2 to #2:")
    assert supervisor.runtime.data["hook"]["cursor"] == 2


@pytest.mark.asyncio
async def test_a_removed_session_is_started_again(supervisor):
    ticket, entry = await started(supervisor)
    await supervisor.terminals.sessions[entry["id"]].stop()
    await supervisor.dispatch(
        {"op": "session_delete", "terminal": entry["id"], "confirmed": True}
    )
    assert hook_entries(supervisor) == []
    supervisor.board.move(ticket.id, "todo")
    await wake(supervisor)
    [fresh] = hook_entries(supervisor)
    assert fresh["id"] != entry["id"]
    assert "Tell me about moves" in fresh["prompt"]
    assert "moved #" in fresh["prompt"]


@pytest.mark.asyncio
async def test_events_are_delivered_in_batches_of_thirty(supervisor):
    await wake(supervisor)
    for number in range(40):
        supervisor.board.create(f"Task {number}")
    await wake(supervisor)
    [entry] = hook_entries(supervisor)
    assert entry["prompt"].count("created #") == 30
    assert supervisor.runtime.data["hook"]["cursor"] == 30
    await on_screen(supervisor, entry["id"], "READY")
    await asyncio.sleep(0.4)
    await wake(supervisor)
    await on_screen(supervisor, entry["id"], "ECHO:Kanbeat board events #31 to #40:")
    assert supervisor.runtime.data["hook"]["cursor"] == 40


@pytest.mark.asyncio
async def test_an_empty_prompt_turns_the_hook_off_and_turning_it_on_starts_fresh(
    supervisor,
):
    supervisor.runtime.save_config({"transition_hook": {"prompt": ""}})
    await wake(supervisor)
    supervisor.board.create("While off")
    await wake(supervisor)
    assert hook_entries(supervisor) == []
    assert "cursor" not in supervisor.runtime.data["hook"]
    supervisor.runtime.save_config({"transition_hook": {"prompt": "Back on"}})
    await wake(supervisor)
    assert hook_entries(supervisor) == []
    supervisor.board.create("After on")
    await wake(supervisor)
    [entry] = hook_entries(supervisor)
    assert "After on" in entry["prompt"] and "While off" not in entry["prompt"]


@pytest.mark.asyncio
async def test_an_unavailable_harness_is_reported_once_and_retried_later(
    supervisor, monkeypatch
):
    await wake(supervisor)
    monkeypatch.setattr(hooks, "available", lambda name: False)
    supervisor.board.create("Task")
    await wake(supervisor)
    for _ in range(3):
        supervisor.subscriber.retry_at = 0.0
        await wake(supervisor)
    notes = [n["message"] for n in supervisor.runtime.data["notices"]]
    assert notes == ["Transition hook: harness claude is unavailable"]
    assert hook_entries(supervisor) == []
    assert supervisor.subscriber.retry_at > 0
    await supervisor.subscriber.step()
    monkeypatch.setattr(hooks, "available", lambda name: True)
    supervisor.subscriber.retry_at = 0.0
    await wake(supervisor)
    assert len(hook_entries(supervisor)) == 1


@pytest.mark.asyncio
async def test_a_hook_prompt_that_cannot_start_a_session_is_reported(
    supervisor, monkeypatch
):
    await wake(supervisor)
    monkeypatch.setattr(hooks, "PROMPT_BYTES", 100)
    supervisor.board.create("Task")
    await wake(supervisor)
    notes = [n["message"] for n in supervisor.runtime.data["notices"]]
    assert notes == [
        "Transition hook: the hook prompt is too long to start the session"
    ]
    assert hook_entries(supervisor) == []
    supervisor.subscriber.retry_at = 0.0
    await wake(supervisor)
    assert len(supervisor.runtime.data["notices"]) == 1


@pytest.mark.asyncio
async def test_the_hook_does_not_need_the_board_to_run(supervisor):
    assert supervisor.runtime.data["execution"] == "stopped"
    await started(supervisor)
    assert supervisor.runtime.data["execution"] == "stopped"


def test_invalid_hook_configuration_is_not_saved(supervisor):
    for value in (None, [], {"harness": "unknown"}, {"prompt": 5}):
        with pytest.raises(ValueError):
            supervisor.runtime.save_config(
                {"global_cp": "Changed", "transition_hook": value}
            )
    assert supervisor.runtime.config["global_cp"] == ""
    with pytest.raises(ValueError, match="longer than 8000"):
        supervisor.runtime.change_config({"transition_hook.prompt": "x" * 8001}, "cli")


def test_invalid_hook_config_file_rejected_at_startup(tmp_path):
    instance = Supervisor(tmp_path)
    instance.runtime.config_path.write_text('{"transition_hook": {"prompt": 123}}')
    with pytest.raises(ValueError, match="prompt must be text"):
        Supervisor(tmp_path)


@pytest.mark.asyncio
async def test_a_change_made_right_after_enabling_the_hook_is_not_lost(supervisor):
    supervisor.runtime.change_config({"transition_hook.prompt": ""}, "cli")
    await wake(supervisor)
    supervisor.runtime.change_config({"transition_hook.prompt": "Watch"}, "cli")
    assert supervisor.runtime.data["hook"]["cursor"] == supervisor.events.last
    supervisor.board.create("Right away")
    await supervisor.reconcile()
    await supervisor.subscriber.guarded_step()
    [entry] = hook_entries(supervisor)
    assert "Right away" in entry["prompt"] and "Watch" in entry["prompt"]
    assert supervisor.runtime.data["hook"]["cursor"] == supervisor.events.last

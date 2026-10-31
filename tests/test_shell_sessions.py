import asyncio
import os
from pathlib import Path

import pytest

from kanbeat.native import process_directory
from kanbeat.supervisor import Supervisor


@pytest.fixture
def supervisor(tmp_path, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setattr("kanbeat.terminals.CWD_POLL_SECONDS", 0.05)
    return Supervisor(tmp_path)


async def eventually(predicate, message, seconds=10.0):
    for _ in range(int(seconds / 0.05)):
        if predicate():
            return
        await asyncio.sleep(0.05)
    pytest.fail(message)


async def type_line(supervisor, terminal_id, line):
    for event in (
        {"kind": "paste", "text": line},
        {"kind": "key", "key": "enter", "character": "\r"},
    ):
        await supervisor.dispatch(
            {"op": "terminal_input", "terminal": terminal_id, **event}
        )


def test_process_directory_reads_a_running_process():
    assert process_directory(os.getpid()) == os.path.realpath(os.getcwd())
    assert process_directory(2**22 + 12345) is None


@pytest.mark.asyncio
async def test_terminal_session_starts_in_the_project_and_keeps_its_directory(
    supervisor, tmp_path
):
    target = (tmp_path / "work tree").resolve()
    target.mkdir()
    terminal_id = (
        await supervisor.dispatch(
            {"op": "session_create", "harness": "shell", "title": "Shell"}
        )
    )["terminal"]
    entry = supervisor.terminals.entries[terminal_id]
    try:
        assert entry["harness"] == "shell"
        assert not entry.get("native_session")
        await eventually(lambda: "cwd" in entry, "the start directory is recorded")
        assert entry["cwd"] == os.path.realpath(tmp_path)
        await type_line(supervisor, terminal_id, f"cd '{target}'")
        await eventually(lambda: entry.get("cwd") == str(target), "cd is tracked")

        await supervisor.dispatch(
            {"op": "terminal_stop", "terminal": terminal_id, "confirmed": True}
        )
        await eventually(lambda: entry["status"] == "stopped", "the shell stops")
        assert entry["cwd"] == str(target)

        await supervisor.dispatch({"op": "session_restart", "terminal": terminal_id})
        again = supervisor.terminals.get(terminal_id)
        await type_line(supervisor, terminal_id, "pwd")
        squeeze = lambda text: "".join(text.split())
        await eventually(
            lambda: squeeze(str(target)) in squeeze(again.snapshot()["text"]),
            "the restarted shell opens in the saved directory",
        )
    finally:
        await supervisor.terminals.stop()


@pytest.mark.asyncio
async def test_a_removed_saved_directory_falls_back_to_the_project(
    supervisor, tmp_path
):
    gone = (tmp_path / "gone").resolve()
    gone.mkdir()
    terminal_id = (
        await supervisor.dispatch(
            {"op": "session_create", "harness": "shell", "title": "Shell"}
        )
    )["terminal"]
    entry = supervisor.terminals.entries[terminal_id]
    try:
        await type_line(supervisor, terminal_id, f"cd '{gone}'")
        await eventually(lambda: entry.get("cwd") == str(gone), "cd is tracked")
        await supervisor.dispatch(
            {"op": "terminal_stop", "terminal": terminal_id, "confirmed": True}
        )
        await eventually(lambda: entry["status"] == "stopped", "the shell stops")
        gone.rmdir()
        await supervisor.dispatch({"op": "session_restart", "terminal": terminal_id})
        await eventually(
            lambda: entry.get("cwd") == os.path.realpath(tmp_path),
            "the shell starts in the project",
        )
    finally:
        await supervisor.terminals.stop()


@pytest.mark.asyncio
async def test_terminal_sessions_take_no_prompt_or_native_id(supervisor):
    with pytest.raises(ValueError, match="no prompt"):
        await supervisor.dispatch(
            {"op": "session_create", "harness": "shell", "prompt": "hi"}
        )
    terminal_id = (
        await supervisor.dispatch({"op": "session_create", "harness": "shell"})
    )["terminal"]
    try:
        await supervisor.dispatch(
            {"op": "terminal_stop", "terminal": terminal_id, "confirmed": True}
        )
        with pytest.raises(ValueError, match="no native session"):
            await supervisor.dispatch(
                {
                    "op": "session_update",
                    "terminal": terminal_id,
                    "title": "Shell",
                    "native_session": "abc",
                }
            )
        assert isinstance(supervisor.terminals.entries[terminal_id]["title"], str)
    finally:
        await supervisor.terminals.stop()
        assert Path(supervisor.board.project).exists()


@pytest.mark.asyncio
async def test_directory_tracking_saves_state_only_on_the_event_loop_thread(
    supervisor, tmp_path, monkeypatch
):
    import threading

    loop_thread = threading.get_ident()
    save_threads = []
    original = supervisor.runtime.save

    def save():
        save_threads.append(threading.get_ident())
        original()

    monkeypatch.setattr(supervisor.runtime, "save", save)
    target = (tmp_path / "elsewhere").resolve()
    target.mkdir()
    terminal_id = (
        await supervisor.dispatch({"op": "session_create", "harness": "shell"})
    )["terminal"]
    entry = supervisor.terminals.entries[terminal_id]
    try:
        await type_line(supervisor, terminal_id, f"cd '{target}'")
        await eventually(lambda: entry.get("cwd") == str(target), "cd is tracked")
        assert save_threads and set(save_threads) == {loop_thread}
    finally:
        await supervisor.terminals.stop()

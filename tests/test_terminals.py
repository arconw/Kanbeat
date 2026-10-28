import asyncio
import sys

import pytest

from kanbeat.native import process_identity
from kanbeat.supervisor import Supervisor
from kanbeat.terminals import interactive_command
from kanbeat.ui.app import KanbeatApp
from kanbeat.ui.terminal import TerminalDisplay, TerminalView

SCRIPT = """
import os, sys
with open('/dev/tty') as terminal:
    print('TTY', os.isatty(0), os.isatty(1), terminal.isatty(), flush=True)
print('\\x1b[31mREADY\\x1b[0m', flush=True)
for line in sys.stdin:
    if line.strip() == 'size':
        size = os.get_terminal_size(0)
        print('SIZE', size.columns, size.lines, flush=True)
    elif line.strip() == 'quit':
        break
    else:
        print('ECHO:' + line.strip(), flush=True)
"""


@pytest.fixture
def supervisor(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "kanbeat.terminals.interactive_command",
        lambda *args: [sys.executable, "-u", "-c", SCRIPT],
    )
    monkeypatch.setattr("kanbeat.supervisor.available", lambda name: True)
    return Supervisor(tmp_path)


async def wait_text(session, text):
    for _ in range(200):
        if text in session.snapshot()["text"]:
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"Missing terminal text {text!r}: {session.snapshot()['text']}")


@pytest.mark.asyncio
async def test_real_pty_input_resize_and_reconnect(supervisor):
    ticket = supervisor.board.create("Task")
    try:
        result = await supervisor.dispatch({"op": "terminal_open", "ticket": ticket.id})
        terminal_id = result["terminal"]
        session = supervisor.terminals.get(terminal_id)
        await wait_text(session, "TTY True True True")
        await supervisor.dispatch(
            {
                "op": "terminal_input",
                "terminal": terminal_id,
                "kind": "paste",
                "text": "Привет\n",
            }
        )
        await wait_text(session, "ECHO:Привет")
        again = await supervisor.dispatch({"op": "terminal_open", "ticket": ticket.id})
        assert again == result
        await supervisor.dispatch(
            {"op": "terminal_resize", "terminal": terminal_id, "rows": 30, "cols": 100}
        )
        await supervisor.dispatch(
            {
                "op": "terminal_input",
                "terminal": terminal_id,
                "kind": "paste",
                "text": "size\n",
            }
        )
        await wait_text(session, "SIZE 100 30")
        with pytest.raises(ValueError, match="confirmation"):
            await supervisor.dispatch({"op": "terminal_stop", "terminal": terminal_id})
        await supervisor.dispatch(
            {"op": "terminal_stop", "terminal": terminal_id, "confirmed": True}
        )
        assert process_identity(session.process.pid) is None
        assert session.fd is None
        assert session.entry["status"] == "stopped"
    finally:
        await supervisor.terminals.stop()


@pytest.mark.asyncio
async def test_board_stop_does_not_stop_independent_terminal(supervisor):
    board_id = await supervisor.terminals.create("codex", owner="board")
    custom_id = await supervisor.terminals.create("claude", owner="custom")
    try:
        await supervisor.stop()
        assert supervisor.terminals.get(board_id).entry["status"] == "stopped"
        assert supervisor.terminals.get(custom_id).entry["status"] == "running"
    finally:
        await supervisor.terminals.stop()


@pytest.mark.asyncio
async def test_play_requires_task_terminal_to_stop(supervisor):
    ticket = supervisor.board.create("Task")
    try:
        await supervisor.dispatch({"op": "terminal_open", "ticket": ticket.id})
        with pytest.raises(ValueError, match="Stop interactive"):
            await supervisor.play()
        await supervisor.stop()
        await supervisor.play()
        with pytest.raises(ValueError, match="Stop the board"):
            await supervisor.dispatch({"op": "terminal_open", "ticket": ticket.id})
    finally:
        await supervisor.stop()


@pytest.mark.asyncio
async def test_terminal_modal_detach_keeps_process(supervisor, monkeypatch):
    ticket = supervisor.board.create("Task")
    terminal_id = (
        await supervisor.dispatch({"op": "terminal_open", "ticket": ticket.id})
    )["terminal"]
    session = supervisor.terminals.get(terminal_id)
    app = KanbeatApp(supervisor.board.project)
    monkeypatch.setattr(app, "rpc", supervisor.dispatch)
    try:
        await wait_text(session, "READY")
        async with app.run_test(size=(100, 40)) as pilot:
            await app.push_screen(TerminalView(terminal_id))
            await pilot.pause()
            assert app.screen.query_one(TerminalDisplay).has_focus
            await pilot.press("h", "i", "enter")
            await wait_text(session, "ECHO:hi")
            await pilot.click("#terminal-detach")
            await pilot.pause()
            assert session.entry["status"] == "running"
            await app.push_screen(TerminalView(terminal_id))
            await pilot.pause()
            assert "ECHO:hi" in session.snapshot()["text"]
            await pilot.press("ctrl+backslash")
            await pilot.pause()
            assert not isinstance(app.screen, TerminalView)
        assert session.entry["status"] == "running"
    finally:
        await supervisor.terminals.stop()


@pytest.mark.asyncio
async def test_terminal_identity_survives_restart(supervisor):
    terminal_id = await supervisor.terminals.create(
        "codex", native_session="native-123"
    )
    await supervisor.terminals.stop()
    restored = Supervisor(supervisor.board.project)
    await restored.recover_orphans()
    entry = restored.terminals.entries[terminal_id]
    assert entry["native_session"] == "native-123"
    assert entry["status"] == "stopped"
    assert restored.runtime.data["execution"] == "stopped"


@pytest.mark.asyncio
async def test_external_move_stops_task_terminal(supervisor):
    ticket = supervisor.board.create("Task")
    terminal_id = (
        await supervisor.dispatch({"op": "terminal_open", "ticket": ticket.id})
    )["terminal"]
    supervisor.board.move(ticket.id, "archive")
    await supervisor.reconcile()
    assert supervisor.terminals.get(terminal_id).entry["status"] == "stopped"


@pytest.mark.parametrize("name", ["codex", "claude", "opencode", "pi"])
def test_interactive_commands_preserve_native_security(name):
    for session in (None, "native-session"):
        argv = interactive_command(name, session, "Read the ticket")
        assert argv[0] == name
        assert not {
            "--yolo",
            "--auto",
            "--dangerously-skip-permissions",
            "--dangerously-bypass-approvals-and-sandbox",
        }.intersection(argv)
        assert ("native-session" in argv) == bool(session)


@pytest.mark.asyncio
async def test_concurrent_clients_share_one_task_terminal(supervisor):
    ticket = supervisor.board.create("Task")
    try:
        results = await asyncio.gather(
            *(
                supervisor.dispatch({"op": "terminal_open", "ticket": ticket.id})
                for _ in range(2)
            )
        )
        assert results[0] == results[1]
        assert len(supervisor.terminals.sessions) == 1
    finally:
        await supervisor.terminals.stop()


@pytest.mark.asyncio
async def test_terminal_exit_is_reaped(supervisor):
    terminal_id = await supervisor.terminals.create("codex")
    session = supervisor.terminals.get(terminal_id)
    await wait_text(session, "READY")
    session.write("quit\n")
    await asyncio.wait_for(session.waiter, timeout=5)
    assert session.entry["status"] == "exited"
    assert session.entry["exit_code"] == 0
    assert session.fd is None
    assert process_identity(session.process.pid) is None


@pytest.mark.asyncio
async def test_log_open_failure_closes_pty(supervisor, monkeypatch):
    from pathlib import Path

    original = Path.open

    def fail_log(path, *args, **kwargs):
        if path.name.startswith("terminal-"):
            raise OSError("Log unavailable")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_log)
    with pytest.raises(OSError, match="Log unavailable"):
        await supervisor.terminals.create("codex")
    session = next(iter(supervisor.terminals.sessions.values()))
    assert session.fd is None
    assert session.process is None
    assert session.entry["status"] == "failed"


@pytest.mark.asyncio
async def test_invalid_terminal_size_is_rejected(supervisor):
    terminal_id = await supervisor.terminals.create("codex")
    try:
        for rows, cols in ((0, 80), (24, 0), (1000, 80), (24, 1000)):
            with pytest.raises(ValueError, match="Terminal size"):
                await supervisor.dispatch(
                    {
                        "op": "terminal_resize",
                        "terminal": terminal_id,
                        "rows": rows,
                        "cols": cols,
                    }
                )
    finally:
        await supervisor.terminals.stop()


@pytest.mark.asyncio
async def test_slow_spawn_serializes_other_client_open(supervisor, monkeypatch):
    from kanbeat.terminals import TerminalSession

    entered = asyncio.Event()
    release = asyncio.Event()
    original = TerminalSession.start

    async def delayed_start(session, argv):
        entered.set()
        await release.wait()
        await original(session, argv)

    monkeypatch.setattr(TerminalSession, "start", delayed_start)
    ticket = supervisor.board.create("Task")
    first = asyncio.create_task(
        supervisor.dispatch({"op": "terminal_open", "ticket": ticket.id})
    )
    await entered.wait()
    second = asyncio.create_task(
        supervisor.dispatch({"op": "terminal_open", "ticket": ticket.id})
    )
    await asyncio.sleep(0)
    try:
        assert len(supervisor.terminals.entries) == 1
        assert next(iter(supervisor.terminals.entries.values()))["status"] == "starting"
        assert not second.done()
        release.set()
        results = await asyncio.gather(first, second)
        assert results[0] == results[1]
        assert len(supervisor.terminals.sessions) == 1
    finally:
        release.set()
        await asyncio.gather(first, second, return_exceptions=True)
        await supervisor.terminals.stop()

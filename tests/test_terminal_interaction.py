import asyncio
import sys
from types import SimpleNamespace

import pytest
import pytest_asyncio
from textual import events

from kanbeat.supervisor import Supervisor
from kanbeat.terminal_control import InputCapture, apply_input
from kanbeat.terminal_history import HISTORY_LINES, HistoryBoard
from kanbeat.ui.app import KanbeatApp
from kanbeat.ui.terminal import TerminalDisplay, TerminalView
from kanbeat.ui.terminal_input import MAX_QUEUED_INPUTS, TerminalInput

RAW_CLI = """
import os, sys, tty
tty.setraw(0)
print('\\x1b[?1000h\\x1b[?1006h\\x1b[?2004hREADY\\r', flush=True)
pending = b''
while True:
    pending += os.read(0, 4096)
    while b'\\n' in pending:
        line, pending = pending.split(b'\\n', 1)
        print('INPUTHEX:' + line.hex() + '\\r', flush=True)
"""


@pytest_asyncio.fixture
async def terminal(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "kanbeat.terminals.interactive_command",
        lambda *args: [sys.executable, "-u", "-c", RAW_CLI],
    )
    monkeypatch.setattr("kanbeat.supervisor.available", lambda name: True)
    supervisor = Supervisor(tmp_path)
    ticket = supervisor.board.create("Task")
    terminal_id = (
        await supervisor.dispatch({"op": "terminal_open", "ticket": ticket.id})
    )["terminal"]
    session = supervisor.terminals.get(terminal_id)
    for _ in range(200):
        if "READY" in session.snapshot()["text"]:
            break
        await asyncio.sleep(0.01)
    assert "READY" in session.snapshot()["text"]
    try:
        yield supervisor, session
    finally:
        await supervisor.terminals.stop()


async def contains(session, text):
    for _ in range(200):
        if text in session.snapshot()["text"]:
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"Missing expected terminal output: {text}")


@pytest.mark.asyncio
async def test_real_pty_mouse_protocol_and_bracketed_unicode_paste(terminal):
    supervisor, session = terminal
    await supervisor.dispatch(
        {
            "op": "terminal_input",
            "terminal": session.id,
            "events": [
                {
                    "kind": "wheel",
                    "direction": "up",
                    "x": 2,
                    "y": 3,
                    "modifiers": ["ctrl"],
                },
                {"kind": "paste", "text": "Привет"},
                {"kind": "key", "key": "ctrl+j", "character": "\n"},
            ],
        }
    )
    expected = b"\x1b[<80;2;3M\x1b[200~" + "Привет".encode() + b"\x1b[201~"
    await contains(session, "INPUTHEX:" + expected.hex())


@pytest.mark.asyncio
async def test_invalid_input_batch_is_rejected_before_any_write(terminal, monkeypatch):
    supervisor, session = terminal
    writes = []
    monkeypatch.setattr(
        session, "queue_bytes", lambda data: writes.append(data) or True
    )
    result = await supervisor.dispatch(
        {
            "op": "terminal_input",
            "terminal": session.id,
            "events": [
                {"kind": "key", "character": "x"},
                {"kind": "key", "key": "invalid+x"},
            ],
        }
    )
    assert not result["accepted"]
    assert result["rejected"][0]["index"] == 1
    assert "modifier" in result["rejected"][0]["error"]
    assert writes == []
    assert session.board.host.connection is session


@pytest.mark.asyncio
async def test_terminal_wheel_reaches_native_app_and_fills_bottom(
    terminal, monkeypatch
):
    supervisor, session = terminal
    app = KanbeatApp(supervisor.board.project)
    monkeypatch.setattr(app, "rpc", supervisor.dispatch)
    async with app.run_test(size=(100, 40)) as pilot:
        view = TerminalView(session.id)
        app.push_screen(view)
        await pilot.pause()
        display = view.query_one(TerminalDisplay)
        assert display.region.bottom == 40
        assert display.region.y == 4
        assert session.entry["rows"] == display.size.height
        display.post_message(
            events.MouseScrollUp(display, 3, 2, 0, 0, 0, False, False, False)
        )
        await pilot.pause()
        await view.send_input({"kind": "key", "key": "ctrl+j", "character": "\n"})
        await view.input.drain()
        await contains(session, "INPUTHEX:1b5b3c36343b343b334d")
        await view.action_detach()
        assert session.entry["status"] == "running"


@pytest.mark.asyncio
async def test_input_burst_preserves_order_under_backpressure():
    ready = asyncio.Event()
    delivered = []
    failures = []

    async def send(op, events):
        if not ready.is_set():
            return {"accepted": False}
        delivered.extend(event["character"] for event in events)
        return {"accepted": True}

    queue = TerminalInput(send, failures.append)
    pump = asyncio.create_task(queue.run())
    expected = [chr(97 + index % 26) for index in range(1200)]

    async def produce():
        for character in expected:
            await queue.put({"kind": "key", "character": character})

    producer = asyncio.create_task(produce())
    try:
        await asyncio.sleep(0.03)
        assert queue.queue.qsize() == MAX_QUEUED_INPUTS
        assert not producer.done()
        ready.set()
        await asyncio.wait_for(producer, timeout=3)
        await queue.drain()
        assert delivered == expected
        assert not failures
    finally:
        producer.cancel()
        pump.cancel()
        await asyncio.gather(producer, pump, return_exceptions=True)


def test_primary_scrollback_is_bounded_and_keeps_absolute_position():
    board = HistoryBoard(width=20, height=3)
    for index in range(HISTORY_LINES + 20):
        board.parser.feed(f"line-{index}\r\n")
    assert len(board.history) == HISTORY_LINES
    assert board.history_start > 0
    position, before = board.history_window(board.history_total - 10)
    board.parser.feed("new output\r\n")
    assert board.history_window(position) == (position, before)
    oldest, text = board.history_window(-100)
    assert oldest == board.history_start
    assert len(text.split("\n")) == board.height
    count = board.history_total
    board.parser.feed("\x1b[?1049h" + "alternate\r\n" * 8)
    assert board.history_total == count


def test_legacy_mouse_bytes_are_not_reencoded_as_utf8():
    board = HistoryBoard(width=240, height=10)
    capture = InputCapture()
    board.host.attach(capture)
    board.parser.feed("\x1b[?1000h\x1b[?1006l")
    apply_input(
        SimpleNamespace(board=board),
        {"kind": "wheel", "direction": "up", "x": 200, "y": 3},
    )
    assert capture.data == b"\x1b[M\x60\xe8\x23"


@pytest.mark.asyncio
async def test_history_view_stays_anchored_during_output_and_returns_live(
    terminal, monkeypatch
):
    supervisor, session = terminal
    session.board.parser.feed(
        "\r\n" + "\r\n".join(f"LINE-{index:03}" for index in range(70)) + "\r\n"
    )
    session.touch()
    app = KanbeatApp(supervisor.board.project)
    monkeypatch.setattr(app, "rpc", supervisor.dispatch)
    async with app.run_test(size=(100, 40)) as pilot:
        view = TerminalView(session.id)
        app.push_screen(view)
        await pilot.pause()
        event = events.MouseScrollUp(
            view.query_one(TerminalDisplay), 1, 1, 0, 0, 0, True, False, False
        )
        await view.scroll_terminal("up", event)
        assert view.last_snapshot["historical"]
        position = view.scroll_position
        first_row = view.last_snapshot["screen"].split("\n")[0]
        session.board.parser.feed("MORE\r\n" * 4)
        session.touch()
        await pilot.pause()
        assert view.scroll_position == position
        assert view.last_snapshot["screen"].split("\n")[0] == first_row
        assert not view.last_snapshot["cursor_visible"]
        await view.go_live()
        assert view.scroll_position is None
        assert not view.last_snapshot["historical"]


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [float("nan"), float("inf")])
async def test_terminal_wait_rejects_nonfinite_timeout(terminal, timeout):
    supervisor, session = terminal
    with pytest.raises(ValueError, match="finite"):
        await supervisor.dispatch(
            {
                "op": "terminal_wait",
                "terminal": session.id,
                "revision": session.revision,
                "timeout": timeout,
            }
        )


@pytest.mark.parametrize(
    "key,expected", [("pageup", b"\x1b[5~"), ("pagedown", b"\x1b[6~")]
)
def test_page_navigation_uses_native_keyboard_sequences(key, expected):
    board = HistoryBoard(width=80, height=24)
    capture = InputCapture()
    board.host.attach(capture)
    apply_input(SimpleNamespace(board=board), {"kind": "key", "key": key})
    assert capture.data == expected


@pytest.mark.asyncio
async def test_rejected_event_does_not_disable_input_or_drop_valid_keys(terminal):
    supervisor, session = terminal
    failures = []

    async def send(op, **values):
        return await supervisor.dispatch({"op": op, "terminal": session.id, **values})

    queue = TerminalInput(send, failures.append)
    for value in [
        {"kind": "key", "character": "a"},
        {"kind": "paste", "text": "x" * 16001},
        {"kind": "key", "character": "b"},
        {"kind": "key", "key": "ctrl+j", "character": "\n"},
    ]:
        await queue.put(value)
    pump = asyncio.create_task(queue.run())
    try:
        await queue.drain()
        await contains(session, "INPUTHEX:6162")
        assert failures and not queue.error
        await queue.put({"kind": "key", "character": "c"})
        await queue.put({"kind": "key", "key": "ctrl+j", "character": "\n"})
        await queue.drain()
        await contains(session, "INPUTHEX:63")
    finally:
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)


@pytest.mark.asyncio
async def test_automatic_terminal_replies_are_safe_after_close_and_backpressure(
    terminal, monkeypatch
):
    _, session = terminal
    monkeypatch.setattr(session, "queue_bytes", lambda payload: False)
    session.board.parser.feed("\x1b[c\x1b[6n")
    session.close()
    session.board.parser.feed("\x1b[c\x1b[6n")
    assert session.fd is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields", [{"revision": None}, {"timeout": {}}, {"revision": {}}]
)
async def test_terminal_wait_rejects_bad_numeric_fields(terminal, fields):
    supervisor, session = terminal
    with pytest.raises(ValueError, match="numeric"):
        await supervisor.dispatch(
            {
                "op": "terminal_wait",
                "terminal": session.id,
                "revision": session.revision,
                **fields,
            }
        )

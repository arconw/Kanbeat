import asyncio
import sys
from types import SimpleNamespace

import pytest
import pytest_asyncio
from textual import events
from textual.widgets import Static

from kanbeat.supervisor import Supervisor
from kanbeat.terminal_control import InputCapture, apply_input
from kanbeat.terminal_history import HISTORY_LINES, HistoryBoard
from kanbeat.ui.app import KanbeatApp
from kanbeat.ui.terminal import TerminalDisplay, TerminalPane, TerminalView
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

ALT_VIEWER_CLI = """
import os, sys, tty
tty.setraw(0)
sys.stdout.write("\\x1b[?1049h\\x1b[?1000h\\x1b[?1006h")
for index in range(80):
    sys.stdout.write(f"ALT-{index:03}\\r\\n")
sys.stdout.write("VIEWER-READY\\r\\n")
sys.stdout.flush()
while True:
    data = os.read(0, 4096)
    sys.stdout.write("VIEWER-GOT:" + data.hex() + "\\r\\n")
    sys.stdout.flush()
"""

INLINE_MONITOR_CLI = """
import time
for index in range(80):
    print(f"INLINE-{index:03}", flush=True)
time.sleep(60)
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


async def contains(session, text, count=1):
    for _ in range(200):
        if session.snapshot()["text"].count(text) >= count:
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"Missing expected terminal output: {text}")


@pytest_asyncio.fixture
async def monitor_terminal_factory(tmp_path, monkeypatch):
    monkeypatch.setattr("kanbeat.supervisor.available", lambda name: True)
    supervisors = []
    jobs = []

    async def start(script, slot="developer"):
        project = tmp_path / f"monitor-{len(supervisors)}"
        project.mkdir()
        supervisor = Supervisor(project)
        supervisors.append(supervisor)
        column = "progress" if slot == "developer" else "review"
        ticket = supervisor.board.create("Task", column=column)
        terminal_id = await supervisor.terminals.create(
            "shell",
            ticket=ticket.id,
            column=column,
            slot=slot,
            owner="attach",
            argv=[sys.executable, "-u", "-c", script],
            title="fake viewer",
        )
        session = supervisor.terminals.get(terminal_id)
        job = asyncio.create_task(asyncio.sleep(60))
        jobs.append(job)
        supervisor.jobs[supervisor.slot_key(ticket.id, slot)] = job
        return supervisor, session

    try:
        yield start
    finally:
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        for supervisor in supervisors:
            await supervisor.terminals.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("slot", ["developer", "reviewer-1"])
async def test_monitor_only_slot_terminal_scrolls_the_attached_view(
    slot, monitor_terminal_factory, monkeypatch
):
    supervisor, session = await monitor_terminal_factory(ALT_VIEWER_CLI, slot)
    await contains(session, "VIEWER-READY")
    app = KanbeatApp(supervisor.board.project)
    monkeypatch.setattr(app, "rpc", supervisor.dispatch)
    notices = []
    monkeypatch.setattr(
        app, "notify", lambda message, **kwargs: notices.append((message, kwargs))
    )
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause(0.6)
        view = TerminalView(session.id)
        app.push_screen(view)
        await pilot.pause()
        snapshot = view.last_snapshot
        assert snapshot["read_only"] is True
        assert snapshot["alternate_screen"] is True
        assert snapshot["mouse_tracking"] is True
        assert not view.has_local_history(snapshot)
        display = view.query_one(TerminalDisplay)
        display.post_message(
            events.MouseScrollUp(display, 3, 2, 0, 0, 0, False, False, False)
        )
        await contains(session, "VIEWER-GOT:1b5b3c36343b343b334d")
        await view.scroll_terminal(
            "down",
            events.MouseScrollDown(display, 3, 2, 0, 0, 0, True, False, False),
        )
        await contains(session, "VIEWER-GOT:1b5b3c36353b343b334d")
        assert view.scroll_position is None
        assert not view.last_snapshot["historical"]
        await view.send_input({"kind": "key", "key": "x", "character": "x"})
        await view.send_input({"kind": "paste", "text": "hello"})
        assert "VIEWER-GOT:78" not in session.snapshot()["text"]
        assert "VIEWER-GOT:68656c6c6f" not in session.snapshot()["text"]
        assert any("read-only" in message for message, _ in notices)
        with pytest.raises(ValueError, match="live worker monitor"):
            await supervisor.dispatch(
                {
                    "op": "terminal_input",
                    "terminal": session.id,
                    "kind": "key",
                    "key": "x",
                    "character": "x",
                }
            )
        job = next(iter(supervisor.jobs.values()))
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)
        await view.refresh_view()
        assert view.last_snapshot["read_only"] is False
        await view.scroll_terminal(
            "up",
            events.MouseScrollUp(display, 3, 2, 0, 0, 0, True, False, False),
        )
        await contains(session, "VIEWER-GOT:1b5b3c36343b343b334d", count=2)
        await view.action_detach()
        await pilot.pause()


@pytest.mark.asyncio
async def test_monitor_only_primary_output_keeps_local_history_and_live(
    monitor_terminal_factory, monkeypatch
):
    supervisor, session = await monitor_terminal_factory(INLINE_MONITOR_CLI)
    await contains(session, "INLINE-079")
    app = KanbeatApp(supervisor.board.project)
    monkeypatch.setattr(app, "rpc", supervisor.dispatch)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause(0.6)
        view = TerminalView(session.id)
        app.push_screen(view)
        await pilot.pause()
        assert view.last_snapshot["read_only"] is True
        assert view.has_local_history(view.last_snapshot)
        display = view.query_one(TerminalDisplay)
        display.post_message(
            events.MouseScrollUp(display, 1, 1, 0, 0, 0, False, False, False)
        )
        await pilot.pause()
        assert view.last_snapshot["historical"]
        newest = view.scroll_position
        await view.scroll_terminal(
            "up",
            events.MouseScrollUp(display, 1, 1, 0, 0, 0, True, False, False),
        )
        assert view.scroll_position < newest
        position = view.scroll_position
        first_row = view.last_snapshot["screen"].split("\n")[0]
        session.board.parser.feed("MORE\r\n" * 4)
        session.touch()
        await pilot.pause()
        assert view.scroll_position == position
        assert view.last_snapshot["screen"].split("\n")[0] == first_row
        await view.scroll_terminal(
            "down",
            events.MouseScrollDown(display, 1, 1, 0, 0, 0, True, False, False),
        )
        assert view.scroll_position == newest
        assert view.last_snapshot["historical"]
        await view.go_live()
        assert view.scroll_position is None
        assert not view.last_snapshot["historical"]
        await view.action_detach()
        await pilot.pause()


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
        assert display.region.y == 2
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


@pytest.mark.asyncio
async def test_full_native_input_buffer_backs_off_and_reports_waiting():
    calls = 0
    reports = []

    async def send(op, events):
        nonlocal calls
        calls += 1
        return {"accepted": False}

    queue = TerminalInput(send, reports.append)
    await queue.put({"kind": "key", "character": "a"})
    pump = asyncio.create_task(queue.run())
    try:
        await asyncio.sleep(1.35)
        assert calls < 20
        assert reports == ["Terminal is not accepting input; waiting for space"]
        assert not queue.error
    finally:
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)


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


def test_scrollback_reads_only_evicted_rows_and_ignores_reverse_scroll(monkeypatch):
    board = HistoryBoard(width=20, height=3)
    board.parser.feed("first\r\nsecond\r\nthird")
    monkeypatch.setattr(
        board,
        "capture_text",
        lambda **kwargs: pytest.fail("full-screen capture in scroll path"),
    )
    board.parser.feed("\r\nfourth")
    assert list(board.history) == ["first" + " " * 15]
    count = board.history_total
    board.parser.feed("\x1b[T")
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
    assert session.entry["dropped_replies"] == 2
    await session.stop()
    session.board.parser.feed("\x1b[c\x1b[6n")
    assert session.entry["dropped_replies"] == 2
    assert session.fd is None


@pytest.mark.asyncio
async def test_terminal_wait_wakes_when_supervisor_shuts_down(terminal):
    supervisor, session = terminal
    task = asyncio.create_task(
        supervisor.dispatch(
            {
                "op": "terminal_wait",
                "terminal": session.id,
                "revision": session.revision,
                "timeout": 8,
            }
        )
    )
    await asyncio.sleep(0.01)
    supervisor.shutdown_requested.set()
    result = await asyncio.wait_for(task, timeout=0.3)
    assert result["revision"] == session.revision


@pytest.mark.asyncio
async def test_terminal_view_stops_retrying_permanent_snapshot_errors(
    terminal, monkeypatch
):
    supervisor, session = terminal
    app = KanbeatApp(supervisor.board.project)
    monkeypatch.setattr(app, "rpc", supervisor.dispatch)
    calls = []
    view = TerminalView(session.id)

    async def missing(*args, **kwargs):
        calls.append(args)
        raise ValueError("Terminal is no longer attached")

    monkeypatch.setattr(view, "rpc", missing)
    async with app.run_test(size=(100, 40)) as pilot:
        app.push_screen(view)
        await pilot.pause()
        snapshot_calls = len([call for call in calls if call[0] == "terminal_snapshot"])
        await asyncio.sleep(0.65)
        assert snapshot_calls >= 1
        assert (
            len([call for call in calls if call[0] == "terminal_snapshot"])
            == snapshot_calls
        )
        assert "DISCONNECTED" in str(
            view.query_one("#terminal-heading", Static).render()
        )


@pytest.mark.asyncio
async def test_terminal_view_detaches_quietly_when_native_process_exits(
    terminal, monkeypatch
):
    supervisor, session = terminal
    app = KanbeatApp(supervisor.board.project)
    monkeypatch.setattr(app, "rpc", supervisor.dispatch)
    notices = []
    monkeypatch.setattr(
        app, "notify", lambda message, **kwargs: notices.append((message, kwargs))
    )
    view = TerminalView(session.id)
    async with app.run_test(size=(100, 40)) as pilot:
        app.push_screen(view)
        await pilot.pause()
        await session.stop()
        await view.send_input({"kind": "key", "character": "a"})
        for _ in range(200):
            if app.screen is not view:
                break
            await asyncio.sleep(0.01)
        assert app.screen is not view
        assert not [n for n in notices if n[1].get("severity") == "error"]
        assert any("detached" in message for message, _ in notices)


@pytest.mark.asyncio
async def test_terminal_view_detaches_after_stop_request(terminal, monkeypatch):
    supervisor, session = terminal
    app = KanbeatApp(supervisor.board.project)
    monkeypatch.setattr(app, "rpc", supervisor.dispatch)
    view = TerminalView(session.id)
    async with app.run_test(size=(100, 40)) as pilot:
        app.push_screen(view)
        await pilot.pause()
        await view.rpc("terminal_stop", confirmed=True)
        for _ in range(200):
            if app.screen is not view:
                break
            await asyncio.sleep(0.01)
        assert app.screen is not view
        assert session.entry["status"] == "stopped"


@pytest.mark.asyncio
async def test_dragging_in_the_terminal_selects_and_copies_only_that_text(
    terminal, monkeypatch
):
    supervisor, session = terminal
    app = KanbeatApp(supervisor.board.project)
    monkeypatch.setattr(app, "rpc", supervisor.dispatch)
    copied = []

    async def copy_selection(text):
        copied.append(text)

    monkeypatch.setattr(app, "copy_selection", copy_selection)
    view = TerminalView(session.id)
    async with app.run_test(size=(100, 40)) as pilot:
        app.push_screen(view)
        await pilot.pause(0.5)
        display = view.query_one(TerminalDisplay)
        await pilot.mouse_down(TerminalDisplay, offset=(1, 0))
        await pilot.hover(TerminalDisplay, offset=(3, 0))
        await pilot.mouse_up(TerminalDisplay, offset=(3, 0))
        await pilot.pause()
        assert copied == ["EAD"]
        assert display.selected_span(0) == (1, 4)
        await pilot.click(TerminalDisplay, offset=(10, 0))
        await pilot.pause()
        assert copied == ["EAD"]
        assert display.selected_span(0) is None
        await pilot.mouse_down(TerminalDisplay, offset=(2, 0))
        await pilot.mouse_up(TerminalDisplay, offset=(4, 0))
        await pilot.press("a")
        await pilot.pause()
        assert display.selected_span(0) is None


@pytest.mark.asyncio
async def test_terminal_input_recovers_after_transient_rpc_error_without_replay(
    terminal, monkeypatch
):
    supervisor, session = terminal
    app = KanbeatApp(supervisor.board.project)
    monkeypatch.setattr(app, "rpc", supervisor.dispatch)
    view = TerminalView(session.id)
    original_rpc = view.rpc
    failed = False

    async def flaky(op, **values):
        nonlocal failed
        if op == "terminal_input" and not failed:
            failed = True
            raise OSError("Temporary RPC failure")
        return await original_rpc(op, **values)

    monkeypatch.setattr(view, "rpc", flaky)
    view.input.send = flaky
    async with app.run_test(size=(100, 40)) as pilot:
        app.push_screen(view)
        await pilot.pause()
        old_input = view.input
        await view.send_input({"kind": "key", "character": "a"})
        await old_input.drain()
        assert old_input.error
        session.touch()
        for _ in range(200):
            if view.input is not old_input:
                break
            await asyncio.sleep(0.01)
        assert view.input is not old_input
        await view.send_input({"kind": "key", "character": "b"})
        await view.send_input({"kind": "key", "key": "ctrl+j", "character": "\n"})
        await view.input.drain()
        await contains(session, "INPUTHEX:62")


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


def test_ansi_blue_is_light_enough_to_read_on_the_dark_background():
    display = TerminalDisplay()
    colors = display.board.palette.colors
    assert colors[4] == (95, 175, 255)
    assert colors[12] == (135, 195, 255)
    assert colors[1] == (205, 0, 0)

    def luminance(rgb):
        red, green, blue = (value / 255 for value in rgb)
        return 0.2126 * red + 0.7152 * green + 0.0722 * blue

    assert luminance(colors[4]) > 0.5


def press(key, character, kitty):
    board = HistoryBoard(width=80, height=24)
    capture = InputCapture()
    board.host.attach(capture)
    if kitty:
        board.parser.feed("\x1b[>1u")
    apply_input(
        SimpleNamespace(board=board),
        {"kind": "key", "key": key, "character": character},
    )
    return capture.data


@pytest.mark.parametrize("kitty,expected", [(False, b"\x16"), (True, b"\x1b[118;5u")])
def test_ctrl_v_reaches_the_harness_so_it_can_paste_an_image(kitty, expected):
    assert press("ctrl+v", "\x16", kitty) == expected


@pytest.mark.parametrize("kitty", [False, True])
def test_ctrl_enter_is_a_newline_and_enter_still_submits(kitty):
    assert press("ctrl+enter", "\r", kitty) == b"\n"
    assert press("enter", "\r", kitty) == b"\r"


def test_other_ctrl_letters_keep_their_control_codes():
    assert press("ctrl+a", "\x01", False) == b"\x01"
    assert press("ctrl+c", "\x03", False) == b"\x03"


def hex_cli(kitty):
    enable = "\\x1b[>1u" if kitty else ""
    return f"""
import os, tty
tty.setraw(0)
print('{enable}READY\\r', flush=True)
while True:
    print('GOT:' + os.read(0, 4096).hex() + '\\r', flush=True)
"""


@pytest_asyncio.fixture(params=[False, True], ids=["legacy-harness", "kitty-harness"])
async def keyed_terminal(request, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "kanbeat.terminals.interactive_command",
        lambda *args: [sys.executable, "-u", "-c", hex_cli(request.param)],
    )
    monkeypatch.setattr("kanbeat.supervisor.available", lambda name: True)
    supervisor = Supervisor(tmp_path)
    ticket = supervisor.board.create("Task")
    terminal_id = (
        await supervisor.dispatch({"op": "terminal_open", "ticket": ticket.id})
    )["terminal"]
    session = supervisor.terminals.get(terminal_id)
    await contains(session, "READY")
    try:
        yield supervisor, session, request.param
    finally:
        await supervisor.terminals.stop()


async def press_in_real_pty(supervisor, session, key, character=None):
    await supervisor.dispatch(
        {
            "op": "terminal_input",
            "terminal": session.id,
            "events": [{"kind": "key", "key": key, "character": character}],
        }
    )


@pytest.mark.asyncio
async def test_ctrl_enter_from_a_kitty_terminal_reaches_a_real_pty_as_a_newline(
    keyed_terminal,
):
    supervisor, session, _ = keyed_terminal
    await press_in_real_pty(supervisor, session, "ctrl+enter")
    await contains(session, "GOT:0a")
    await press_in_real_pty(supervisor, session, "enter", "\r")
    await contains(session, "GOT:0d")


@pytest.mark.asyncio
async def test_ctrl_v_reaches_a_real_pty_in_both_keyboard_modes(keyed_terminal):
    supervisor, session, kitty = keyed_terminal
    await press_in_real_pty(supervisor, session, "ctrl+v")
    await contains(session, "GOT:" + ("1b5b3131383b3575" if kitty else "16"))
    await press_in_real_pty(supervisor, session, "ctrl+v", "\x16")
    await contains(session, "GOT:" + ("1b5b3131383b3575" if kitty else "16"), count=2)


@pytest.mark.asyncio
async def test_ctrl_c_is_not_bound_by_kanbeat_and_reaches_the_terminal(
    keyed_terminal, monkeypatch
):
    supervisor, session, kitty = keyed_terminal
    app = KanbeatApp(supervisor.board.project)
    monkeypatch.setattr(app, "rpc", supervisor.dispatch)
    notices = []
    monkeypatch.setattr(
        app, "notify", lambda message, **kwargs: notices.append(message)
    )
    async with app.run_test(size=(100, 40)) as pilot:
        assert "ctrl+c" not in app._bindings.key_to_bindings
        assert "ctrl+p" not in app._bindings.key_to_bindings
        assert "ctrl+q" in app._bindings.key_to_bindings
        await pilot.click("#nav-board")
        await pilot.pause()
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert notices == []
        app.push_screen(TerminalView(session.id))
        await pilot.pause()
        await pilot.pause(0.3)
        await pilot.press("ctrl+c")
        await contains(session, "GOT:" + ("1b5b39393b3575" if kitty else "03"))
        await pilot.press("ctrl+p")
        await contains(session, "GOT:" + ("1b5b3131323b3575" if kitty else "10"))
        assert isinstance(app.screen, TerminalView)
        assert notices == []


@pytest.mark.asyncio
async def test_snapshot_without_a_display_is_applied_after_the_display_returns(
    terminal, monkeypatch
):
    supervisor, session = terminal
    app = KanbeatApp(supervisor.board.project)
    monkeypatch.setattr(app, "rpc", supervisor.dispatch)
    notices = []
    monkeypatch.setattr(
        app, "notify", lambda message, **kwargs: notices.append((message, kwargs))
    )
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.click("#nav-sessions")
        await pilot.pause(0.3)
        pane = app.query_one(TerminalPane)
        pane.open_terminal(session.id)
        await pilot.pause(0.3)
        display = pane.query_one(TerminalDisplay)
        await display.remove()
        await pilot.pause()
        snapshot = await supervisor.dispatch(
            {"op": "terminal_snapshot", "terminal": session.id}
        )
        revision = pane.revision
        assert pane.apply_snapshot(snapshot) is False
        assert pane.revision == revision
        await pane.mount(TerminalDisplay(id="terminal-display"))
        await pilot.pause()
        assert pane.apply_snapshot(snapshot) is True
        assert pane.revision == snapshot["revision"]
        assert not [
            notice for notice in notices if notice[1].get("severity") == "error"
        ]


@pytest.mark.asyncio
async def test_closing_the_pane_cancels_a_pending_snapshot_quietly(
    terminal, monkeypatch
):
    supervisor, session = terminal
    app = KanbeatApp(supervisor.board.project)
    pending = asyncio.Event()
    release = asyncio.Event()

    async def rpc(message):
        if (
            message.get("op") == "terminal_snapshot"
            and message.get("terminal") == session.id
        ):
            pending.set()
            await release.wait()
        return await supervisor.dispatch(message)

    monkeypatch.setattr(app, "rpc", rpc)
    notices = []
    monkeypatch.setattr(
        app, "notify", lambda message, **kwargs: notices.append((message, kwargs))
    )
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.click("#nav-sessions")
        await pilot.pause(0.3)
        pane = app.query_one(TerminalPane)
        pane.open_terminal(session.id)
        await asyncio.wait_for(pending.wait(), timeout=1)
        pane.leave()
        release.set()
        await pilot.pause(0.2)
        assert pane.terminal_id is None
        watchers = [
            worker for worker in pane.workers if worker.group == "terminal-watch"
        ]
        assert all(not worker.is_running for worker in watchers)
        assert not [
            notice for notice in notices if notice[1].get("severity") == "error"
        ]


@pytest.mark.asyncio
async def test_unexpected_watcher_error_is_reported_once_and_stops_the_worker(
    terminal, monkeypatch
):
    supervisor, session = terminal
    app = KanbeatApp(supervisor.board.project)

    async def rpc(message):
        if (
            message.get("op") == "terminal_snapshot"
            and message.get("terminal") == session.id
        ):
            raise KeyError("broken snapshot")
        return await supervisor.dispatch(message)

    monkeypatch.setattr(app, "rpc", rpc)
    notices = []
    monkeypatch.setattr(
        app, "notify", lambda message, **kwargs: notices.append((message, kwargs))
    )
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.click("#nav-sessions")
        await pilot.pause(0.3)
        pane = app.query_one(TerminalPane)
        pane.open_terminal(session.id)
        for _ in range(100):
            if [notice for notice in notices if notice[1].get("severity") == "error"]:
                break
            await asyncio.sleep(0.01)
        await pilot.pause(0.2)
        errors = [notice for notice in notices if notice[1].get("severity") == "error"]
        assert len(errors) == 1
        watchers = [
            worker for worker in pane.workers if worker.group == "terminal-watch"
        ]
        assert all(not worker.is_running for worker in watchers)

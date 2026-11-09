import asyncio
import sys
from types import SimpleNamespace

import pytest
import pytest_asyncio

from kanbeat.supervisor import Supervisor
from kanbeat.terminal_control import (
    MAX_PASTES_PER_TERMINAL,
    PasteAssembler,
    prune_pastes,
)
from kanbeat.ui.terminal_input import TerminalInput, split_paste


def capture_cli(path, *, bracketed=False, sleep=0.0, read_size=65536, exit_after=None):
    enable = "sys.stdout.write('\\x1b[?2004h')" if bracketed else ""
    limit = f"if total >= {exit_after}: break" if exit_after else ""
    return f"""
import os, sys, time, tty
tty.setraw(0)
{enable}
sys.stdout.write('READY\\r\\n')
sys.stdout.flush()
out = open({str(path)!r}, 'wb', buffering=0)
total = 0
while True:
    data = os.read(0, {read_size})
    if not data:
        break
    total += len(data)
    time.sleep({sleep!r})
    out.write(data)
    {limit}
"""


async def start_terminal(tmp_path, monkeypatch, script):
    monkeypatch.setattr(
        "kanbeat.terminals.interactive_command",
        lambda *args, **kwargs: [sys.executable, "-u", "-c", script],
    )
    monkeypatch.setattr("kanbeat.supervisor.available", lambda name: True)
    supervisor = Supervisor(tmp_path)
    ticket = supervisor.board.create("Task")
    terminal_id = (
        await supervisor.dispatch({"op": "terminal_open", "ticket": ticket.id})
    )["terminal"]
    session = supervisor.terminals.get(terminal_id)
    for _ in range(500):
        if "READY" in session.snapshot()["text"]:
            break
        await asyncio.sleep(0.01)
    assert "READY" in session.snapshot()["text"]
    return supervisor, session


async def wait_for_file(path, expected_size, timeout=60.0):
    for _ in range(int(timeout / 0.02)):
        if path.exists() and path.stat().st_size >= expected_size:
            return path.read_bytes()
        await asyncio.sleep(0.02)
    pytest.fail(
        f"File did not reach {expected_size} bytes; got "
        f"{path.stat().st_size if path.exists() else 0}"
    )


def terminal_input(supervisor, session, spool=False):
    async def send(op, **values):
        if not spool:
            # These tests stream the text itself, as a shell session would get it.
            values.pop("file", None)
        return await supervisor.dispatch({"op": op, "terminal": session.id, **values})

    reports = []
    return TerminalInput(send, reports.append), reports


@pytest_asyncio.fixture
async def bracketed_terminal(tmp_path, monkeypatch):
    output = tmp_path / "bracketed.out"
    supervisor, session = await start_terminal(
        tmp_path, monkeypatch, capture_cli(output, bracketed=True)
    )
    try:
        yield supervisor, session, output
    finally:
        await supervisor.terminals.stop()


@pytest_asyncio.fixture
async def plain_terminal(tmp_path, monkeypatch):
    output = tmp_path / "plain.out"
    supervisor, session = await start_terminal(
        tmp_path, monkeypatch, capture_cli(output)
    )
    try:
        yield supervisor, session, output
    finally:
        await supervisor.terminals.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [20 * 1024, 1024 * 1024, 8 * 1024 * 1024])
async def test_large_paste_arrives_complete(bracketed_terminal, size):
    supervisor, session, output = bracketed_terminal
    text = "x" * size
    queue, reports = terminal_input(supervisor, session)
    await queue.put({"kind": "paste", "text": text})
    pump = asyncio.create_task(queue.run())
    try:
        await queue.drain()
        data = await wait_for_file(output, size + 12, timeout=120)
    finally:
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)
    assert reports == []
    assert data == b"\x1b[200~" + text.encode() + b"\x1b[201~"


@pytest.mark.asyncio
async def test_large_paste_has_no_markers_when_bracketed_paste_is_off(
    plain_terminal,
):
    supervisor, session, output = plain_terminal
    text = "y" * (1024 * 1024)
    queue, reports = terminal_input(supervisor, session)
    await queue.put({"kind": "paste", "text": text})
    pump = asyncio.create_task(queue.run())
    try:
        await queue.drain()
        data = await wait_for_file(output, len(text.encode()), timeout=60)
    finally:
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)
    assert reports == []
    assert data == text.encode()
    assert b"\x1b[200~" not in data
    assert b"\x1b[201~" not in data


@pytest.mark.asyncio
async def test_multi_byte_characters_straddling_chunks_arrive_unbroken(
    bracketed_terminal,
):
    supervisor, session, output = bracketed_terminal
    text = "a" * (32768 - 1) + "€" + "b" * 64 + "🙂" * 10000
    queue, reports = terminal_input(supervisor, session)
    await queue.put({"kind": "paste", "text": text})
    pump = asyncio.create_task(queue.run())
    try:
        await queue.drain()
        expected = b"\x1b[200~" + text.encode() + b"\x1b[201~"
        data = await wait_for_file(output, len(expected), timeout=60)
    finally:
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)
    assert reports == []
    assert data == expected


@pytest.mark.asyncio
async def test_slow_reader_still_receives_the_whole_paste(tmp_path, monkeypatch):
    output = tmp_path / "slow.out"
    supervisor, session = await start_terminal(
        tmp_path,
        monkeypatch,
        capture_cli(output, bracketed=False, sleep=0.005, read_size=1024),
    )
    try:
        text = "z" * (512 * 1024)
        queue, reports = terminal_input(supervisor, session)
        await queue.put({"kind": "paste", "text": text})
        pump = asyncio.create_task(queue.run())
        try:
            data = await wait_for_file(output, len(text.encode()), timeout=90)
            await queue.drain()
        finally:
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)
        assert reports == []
        assert data == text.encode()
    finally:
        await supervisor.terminals.stop()


@pytest.mark.asyncio
async def test_small_paste_keeps_the_existing_single_batch_behaviour(
    bracketed_terminal,
):
    supervisor, session, output = bracketed_terminal
    await supervisor.dispatch(
        {
            "op": "terminal_input",
            "terminal": session.id,
            "events": [{"kind": "paste", "text": "Привет"}],
        }
    )
    expected = b"\x1b[200~" + "Привет".encode() + b"\x1b[201~"
    data = await wait_for_file(output, len(expected), timeout=10)
    assert data == expected


@pytest.mark.asyncio
async def test_terminal_ending_discards_an_unfinished_paste(bracketed_terminal):
    supervisor, session, _ = bracketed_terminal
    await supervisor.dispatch(
        {
            "op": "terminal_paste",
            "terminal": session.id,
            "phase": "begin",
            "paste": "p1",
            "client": "c1",
        }
    )
    await supervisor.dispatch(
        {
            "op": "terminal_paste",
            "terminal": session.id,
            "phase": "chunk",
            "paste": "p1",
            "client": "c1",
            "text": "partial",
        }
    )
    assert session.id in supervisor.pastes.terminals
    await session.stop()
    assert session.id not in supervisor.pastes.terminals


def test_stale_paste_is_pruned_and_brackets_are_closed():
    writes = []

    class FakeTerminal:
        id = "t1"

        def __init__(self):
            self.board = SimpleNamespace(modes=SimpleNamespace(bracketed_paste=True))

        def queue_bytes(self, payload):
            writes.append(payload)
            return True

    class FakeSupervisor:
        def __init__(self):
            self.pastes = PasteAssembler()
            self.terminals = SimpleNamespace(sessions={"t1": FakeTerminal()})

    terminal = FakeTerminal()
    supervisor = FakeSupervisor()
    now = 1000.0
    assert supervisor.pastes.begin(terminal, "p1", "c1", now)
    assert supervisor.pastes.chunk(terminal, "p1", "data", "c1", now)
    assert writes == [b"\x1b[200~", b"data"]
    assert supervisor.pastes.prune(now + 61) == [("t1", True)]
    assert supervisor.pastes.terminals == {}


def test_prune_pastes_closes_bracketed_paste_on_live_terminal():
    writes = []

    class FakeTerminal:
        id = "t1"

        def __init__(self):
            self.board = SimpleNamespace(modes=SimpleNamespace(bracketed_paste=True))

        def queue_bytes(self, payload):
            writes.append(payload)
            return True

    class FakeSupervisor:
        def __init__(self):
            self.pastes = PasteAssembler()
            fake = FakeTerminal()
            self.terminals = SimpleNamespace(sessions={"t1": fake})
            self.fake = fake

    supervisor = FakeSupervisor()
    supervisor.pastes.begin(supervisor.fake, "p1", "c1", 1000.0)
    prune_pastes(supervisor, 1000.0 + 61)
    assert writes == [b"\x1b[200~", b"\x1b[201~"]
    assert supervisor.pastes.terminals == {}


@pytest.mark.parametrize(
    "text",
    [
        "a" * 70000,
        "a" * 32767 + "€" + "b" * 40000,
        "🙂" * 20000,
        "текст " * 10000,
    ],
)
def test_split_paste_keeps_characters_whole_and_caps_chunk_bytes(text):
    chunks = list(split_paste(text, 32768))
    assert "".join(chunks) == text
    assert all(len(chunk.encode("utf-8")) <= 32768 for chunk in chunks)


@pytest.mark.asyncio
async def test_paste_interrupted_by_terminal_exit_reports_once_and_cleans_state(
    tmp_path, monkeypatch
):
    output = tmp_path / "exit.out"
    supervisor, session = await start_terminal(
        tmp_path,
        monkeypatch,
        capture_cli(output, bracketed=True, exit_after=100_000),
    )
    try:
        queue, reports = terminal_input(supervisor, session)
        await queue.put({"kind": "paste", "text": "q" * (1024 * 1024)})
        pump = asyncio.create_task(queue.run())
        try:
            await asyncio.wait_for(queue.drain(), timeout=30)
        finally:
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)
        assert session.id not in supervisor.pastes.terminals
        assert len(reports) == 1
        assert "unconfirmed" in reports[0]
    finally:
        await supervisor.terminals.stop()


@pytest.mark.asyncio
async def test_paste_refused_for_a_terminal_that_is_not_running(plain_terminal):
    supervisor, session, _ = plain_terminal
    await session.stop()
    with pytest.raises(ValueError, match="no longer running"):
        await supervisor.dispatch(
            {
                "op": "terminal_paste",
                "terminal": session.id,
                "phase": "begin",
                "paste": "p1",
                "client": "c1",
            }
        )
    assert session.id not in supervisor.pastes.terminals


@pytest.mark.asyncio
async def test_paste_refused_for_a_live_worker_monitor(tmp_path, monkeypatch):
    monkeypatch.setattr("kanbeat.supervisor.available", lambda name: True)
    supervisor = Supervisor(tmp_path)
    ticket = supervisor.board.create("Task", column="progress")
    terminal_id = await supervisor.terminals.create(
        "shell",
        ticket=ticket.id,
        column="progress",
        slot="developer",
        owner="attach",
        argv=[
            sys.executable,
            "-u",
            "-c",
            "import time; print('READY', flush=True); time.sleep(30)",
        ],
    )
    session = supervisor.terminals.get(terminal_id)
    try:
        for _ in range(300):
            if "READY" in session.snapshot()["text"]:
                break
            await asyncio.sleep(0.01)
        monkeypatch.setattr(supervisor, "slot_active", lambda *args: True)
        with pytest.raises(ValueError, match="live worker monitor"):
            await supervisor.dispatch(
                {
                    "op": "terminal_paste",
                    "terminal": session.id,
                    "phase": "begin",
                    "paste": "p1",
                    "client": "c1",
                }
            )
        assert session.id not in supervisor.pastes.terminals
        monkeypatch.setattr(supervisor, "slot_active", lambda *args: False)
        result = await supervisor.dispatch(
            {
                "op": "terminal_paste",
                "terminal": session.id,
                "phase": "begin",
                "paste": "p2",
                "client": "c1",
            }
        )
        assert result == {"accepted": True}
    finally:
        await supervisor.terminals.stop()


def test_abort_keeps_state_until_the_closing_marker_is_queued():
    writes = []

    class FakeTerminal:
        id = "t1"

        def __init__(self):
            self.board = SimpleNamespace(modes=SimpleNamespace(bracketed_paste=True))
            self.allow_end = False

        def queue_bytes(self, payload):
            if payload == b"\x1b[201~" and not self.allow_end:
                return False
            writes.append(payload)
            return True

    terminal = FakeTerminal()
    assembler = PasteAssembler()
    assert assembler.begin(terminal, "p1", "c1", 1000.0)
    assert assembler.chunk(terminal, "p1", "data", "c1", 1000.0)
    assert assembler.abort(terminal, "p1", 1001.0) is False
    assert "t1" in assembler.terminals
    assert assembler.prune(1002.0) == []
    assert "t1" in assembler.terminals
    terminal.allow_end = True
    assert assembler.prune(1003.0) == [("t1", True)]
    assert assembler.terminals == {}
    assert writes == [b"\x1b[200~", b"data", b"\x1b[201~"]


def test_rejected_chunk_refreshes_paste_inactivity():
    class FakeTerminal:
        id = "t1"

        def __init__(self):
            self.board = SimpleNamespace(modes=SimpleNamespace(bracketed_paste=True))

        def queue_bytes(self, payload):
            return payload != b"data"

    terminal = FakeTerminal()
    assembler = PasteAssembler()
    assert assembler.begin(terminal, "p1", "c1", 1000.0)
    assert not assembler.chunk(terminal, "p1", "data", "c1", 1050.0)
    assert assembler.prune(1100.0) == []
    assert "t1" in assembler.terminals
    assert assembler.prune(1111.0) == [("t1", True)]
    assert assembler.terminals == {}


@pytest.mark.asyncio
async def test_cancelled_paste_sends_abort():
    sent = []

    async def send(op, **values):
        sent.append((op, values))
        if values.get("phase") == "chunk":
            await asyncio.sleep(60)
        return {"accepted": True}

    queue = TerminalInput(send, lambda message: None)
    await queue.put({"kind": "paste", "text": "x" * 40000})
    pump = asyncio.create_task(queue.run())
    try:
        for _ in range(200):
            if any(values.get("phase") == "chunk" for _, values in sent):
                break
            await asyncio.sleep(0.01)
        assert any(values.get("phase") == "chunk" for _, values in sent)
    finally:
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)
    assert any(values.get("phase") == "abort" for _, values in sent)


@pytest.mark.asyncio
async def test_pinned_terminal_input_sender_survives_reset():
    from kanbeat.ui.terminal import TerminalSession

    calls = []

    class App:
        async def rpc(self, message):
            calls.append(message)
            return {"accepted": True}

    session = TerminalSession()
    session.app = App()
    sender = session.input_sender("old-terminal")
    session.terminal_id = None
    await sender("terminal_paste", phase="abort", paste="p1")
    assert calls == [
        {
            "op": "terminal_paste",
            "terminal": "old-terminal",
            "phase": "abort",
            "paste": "p1",
        }
    ]


def test_eviction_does_not_close_a_live_paste_mid_stream():
    writes = []

    class FakeTerminal:
        id = "t1"

        def __init__(self):
            self.board = SimpleNamespace(modes=SimpleNamespace(bracketed_paste=True))
            self.allow_end = False

        def queue_bytes(self, payload):
            if payload == b"\x1b[201~" and not self.allow_end:
                return False
            writes.append(payload)
            return True

    terminal = FakeTerminal()
    assembler = PasteAssembler()
    now = 1000.0
    for index in range(MAX_PASTES_PER_TERMINAL):
        assert assembler.begin(terminal, f"p{index}", f"c{index}", now)
    writes.clear()
    assert not assembler.begin(terminal, "new", "new-client", now)
    terminal.allow_end = True
    assert assembler.chunk(terminal, "p0", "more", "c0", now + 1)
    assert assembler.prune(now + 2) == []
    assert "p0" in assembler.terminals["t1"]
    assert b"\x1b[201~" not in writes


def spooled_files(supervisor, session):
    directory = supervisor.state_dir / "pastes" / session.id
    return sorted(directory.glob("*.txt")) if directory.is_dir() else []


async def paste_through_client(supervisor, session, text):
    queue, reports = terminal_input(supervisor, session, spool=True)
    await queue.put({"kind": "paste", "text": text})
    pump = asyncio.create_task(queue.run())
    try:
        await queue.drain()
    finally:
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)
    return reports


@pytest.mark.asyncio
async def test_large_paste_in_an_agent_becomes_a_file_and_a_reference(
    bracketed_terminal,
):
    supervisor, session, output = bracketed_terminal
    text = "".join(f"line {number} \u20ac\n" for number in range(3000))
    assert session.entry["harness"] != "shell"
    reports = await paste_through_client(supervisor, session, text)
    assert reports == []
    (stored,) = spooled_files(supervisor, session)
    assert stored.read_text(encoding="utf-8") == text
    assert stored.stat().st_mode & 0o777 == 0o600
    assert stored.parent.stat().st_mode & 0o777 == 0o700
    sent = await wait_for_file(output, 40, timeout=10)
    for _ in range(100):
        if sent.endswith(b"\x1b[201~"):
            break
        await asyncio.sleep(0.02)
        sent = output.read_bytes()
    assert sent.startswith(b"\x1b[200~[Pasted text: 3000 lines, ")
    assert sent.endswith(b"] \x1b[201~")
    assert str(stored).encode() in sent
    assert b"line 2999" not in sent


@pytest.mark.asyncio
async def test_large_paste_reference_has_no_markers_when_bracketed_paste_is_off(
    plain_terminal,
):
    supervisor, session, output = plain_terminal
    reports = await paste_through_client(supervisor, session, "w" * 20000)
    assert reports == []
    (stored,) = spooled_files(supervisor, session)
    sent = await wait_for_file(output, 40, timeout=10)
    assert b"\x1b[200~" not in sent
    assert sent.startswith(b"[Pasted text: 1 line, 20 KB. Read the whole file ")
    assert str(stored).encode() in sent
    assert stored.read_text() == "w" * 20000


@pytest.mark.asyncio
async def test_paste_below_the_limit_is_typed_into_the_agent(bracketed_terminal):
    supervisor, session, output = bracketed_terminal
    await supervisor.dispatch(
        {
            "op": "terminal_input",
            "terminal": session.id,
            "events": [{"kind": "paste", "text": "short"}],
        }
    )
    assert (await wait_for_file(output, 17, timeout=10)) == b"\x1b[200~short\x1b[201~"
    assert spooled_files(supervisor, session) == []


@pytest.mark.asyncio
async def test_large_paste_in_a_shell_is_still_typed_in_full(tmp_path, monkeypatch):
    output = tmp_path / "shell.out"
    supervisor, session = await start_terminal(
        tmp_path, monkeypatch, capture_cli(output)
    )
    try:
        session.entry["harness"] = "shell"
        text = "s" * 30000
        reports = await paste_through_client(supervisor, session, text)
        assert reports == []
        assert await wait_for_file(output, len(text), timeout=30) == text.encode()
        assert spooled_files(supervisor, session) == []
    finally:
        await supervisor.terminals.stop()


@pytest.mark.asyncio
async def test_abandoned_file_paste_leaves_no_file(bracketed_terminal):
    supervisor, session, output = bracketed_terminal
    now = 100.0
    assert supervisor.pastes.begin(
        session, "p1", "c1", now, supervisor.state_dir / "pastes" / session.id
    )
    assert supervisor.pastes.chunk(session, "p1", "partial", "c1", now)
    assert len(spooled_files(supervisor, session)) == 1
    assert supervisor.pastes.abort(session, "p1", now + 1)
    assert spooled_files(supervisor, session) == []
    assert supervisor.pastes.begin(
        session, "p2", "c1", now, supervisor.state_dir / "pastes" / session.id
    )
    assert len(spooled_files(supervisor, session)) == 1
    supervisor.pastes.discard_terminal(session.id)
    assert spooled_files(supervisor, session) == []
    assert supervisor.pastes.terminals == {}


@pytest.mark.asyncio
async def test_stale_file_paste_is_removed_by_the_pruner(bracketed_terminal):
    supervisor, session, output = bracketed_terminal
    assert supervisor.pastes.begin(
        session, "p1", None, 0.0, supervisor.state_dir / "pastes" / session.id
    )
    assert len(spooled_files(supervisor, session)) == 1
    supervisor.pastes.prune(1000.0, None, supervisor.terminals.sessions)
    assert spooled_files(supervisor, session) == []


def test_remove_pastes_deletes_session_files(tmp_path):
    from kanbeat.terminal_control import paste_directory, remove_pastes

    directory = paste_directory(tmp_path, "abc")
    directory.mkdir(parents=True)
    (directory / "one.txt").write_text("x")
    remove_pastes(tmp_path, "abc")
    assert not directory.exists()
    assert not (tmp_path / "pastes").exists()
    remove_pastes(tmp_path, "abc")

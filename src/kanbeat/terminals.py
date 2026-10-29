from __future__ import annotations

import asyncio
import codecs
import fcntl
import os
import pty
import struct
import sys
import termios
from typing import Any
import uuid

from bittty import Board

from .native import terminate_process, process_identity
from .state import LocalState


def interactive_command(
    harness: str, session: str | None = None, prompt: str = ""
) -> list[str]:
    if session and (
        session.startswith("-")
        or len(session) > 512
        or any(c.isspace() for c in session)
    ):
        raise ValueError("Invalid native session identifier")
    if prompt:
        prompt = "Kanbeat interactive instructions:\n" + prompt
    if harness == "codex":
        return (
            ["codex", "resume", session] + ([prompt] if prompt else [])
            if session
            else ["codex"] + ([prompt] if prompt else [])
        )
    if harness == "claude":
        return (
            ["claude"]
            + (["--resume", session] if session else [])
            + ([prompt] if prompt else [])
        )
    if harness == "opencode":
        return (
            ["opencode"]
            + (["--session", session] if session else [])
            + (["--prompt", prompt] if prompt else [])
        )
    if harness == "pi":
        return (
            ["pi"]
            + (["--session", session] if session else [])
            + ([prompt] if prompt else [])
        )
    raise ValueError("Unsupported interactive harness")


class TerminalSession:
    def __init__(self, state: LocalState, entry: dict[str, Any]):
        self.state = state
        self.entry = entry
        self.id = entry["id"]
        self.board = Board(width=entry.get("cols", 80), height=entry.get("rows", 24))
        self.board.host.attach(self)
        self.process = None
        self.fd: int | None = None
        self.waiter = None
        self.pending = bytearray()
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.log = None
        self.stop_requested = False

    async def start(self, argv: list[str]) -> None:
        master, slave = pty.openpty()
        self.fd = master
        try:
            os.set_blocking(master, False)
            self.resize(self.entry.get("rows", 24), self.entry.get("cols", 80))
            log_path = (
                self.state.board.root / ".state" / "logs" / f"terminal-{self.id}.log"
            )
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self.log = log_path.open("a", encoding="utf-8")
            self.process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "kanbeat.pty_child",
                *argv,
                cwd=self.state.board.project,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
                env={**os.environ, "TERM": "xterm-256color"},
            )
            self.entry.update(
                status="running",
                pid=self.process.pid,
                process_identity=process_identity(self.process.pid),
            )
            asyncio.get_running_loop().add_reader(master, self.read)
            self.waiter = asyncio.create_task(self.wait())
            self.state.save()
        except BaseException:
            await self.stop()
            self.close()
            raise
        finally:
            os.close(slave)

    def write(self, data: str) -> None:
        if self.fd is None:
            return
        payload = data.encode("utf-8")
        if len(self.pending) + len(payload) > 65536:
            raise ValueError("Terminal input queue is full")
        self.pending.extend(payload)
        self.flush()

    def flush(self) -> None:
        if self.fd is None:
            return
        try:
            if self.pending:
                count = os.write(self.fd, self.pending)
                del self.pending[:count]
        except BlockingIOError:
            pass
        except OSError:
            self.pending.clear()
        loop = asyncio.get_running_loop()
        if self.pending:
            loop.add_writer(self.fd, self.flush)
        else:
            loop.remove_writer(self.fd)

    def read(self) -> None:
        if self.fd is None:
            return
        try:
            for _ in range(16):
                data = os.read(self.fd, 65536)
                if not data:
                    self.close()
                    return
                text = self.decoder.decode(data)
                self.board.parser.feed(text)
                if self.log is not None:
                    self.log.write(text)
                    self.log.flush()
        except BlockingIOError:
            pass
        except OSError:
            self.close()

    def resize(self, rows: int, cols: int) -> None:
        if not 1 <= rows <= 100 or not 1 <= cols <= 240:
            raise ValueError("Terminal size must be 1..100 rows and 1..240 columns")
        self.entry.update(rows=rows, cols=cols)
        self.board.resize(cols, rows)
        if self.fd is not None:
            fcntl.ioctl(
                self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0)
            )

    async def wait(self) -> None:
        try:
            code = await self.process.wait()
            self.read()
            self.entry.update(
                status="stopped" if self.stop_requested else "exited", exit_code=code
            )
        finally:
            self.close()
            self.entry.pop("pid", None)
            self.entry.pop("process_identity", None)
            self.state.save()

    async def stop(self) -> None:
        self.stop_requested = True
        if self.process is not None and self.process.returncode is None:
            await terminate_process(self.process)
        if self.waiter is not None:
            await self.waiter

    def close(self) -> None:
        if self.fd is not None:
            loop = asyncio.get_running_loop()
            loop.remove_reader(self.fd)
            loop.remove_writer(self.fd)
            os.close(self.fd)
            self.fd = None
        if self.log is not None:
            self.log.close()
            self.log = None

    def snapshot(self) -> dict[str, Any]:
        return {
            **self.entry,
            "screen": self.board.capture_pane(),
            "cursor": [self.board.cursor.x, self.board.cursor.y],
            "cursor_visible": self.board.modes.cursor_visible,
            "text": self.board.capture_text(),
        }


class Terminals:
    def __init__(self, state: LocalState):
        self.state = state
        self.entries = state.data.setdefault("terminals", {})
        self.sessions: dict[str, TerminalSession] = {}

    async def create(
        self,
        harness: str,
        *,
        ticket: str = "",
        slot: str = "",
        owner: str = "board",
        native_session: str | None = None,
        prompt: str = "",
        column: str = "",
        group: str = "",
        title: str = "",
    ) -> str:
        interactive_command(harness, native_session, prompt)
        terminal_id = uuid.uuid4().hex
        entry = {
            "id": terminal_id,
            "harness": harness,
            "ticket": ticket,
            "column": column,
            "slot": slot,
            "owner": owner,
            "native_session": native_session,
            "group": group,
            "title": title or harness,
            "prompt": prompt,
            "status": "starting",
            "rows": 24,
            "cols": 80,
        }
        self.entries[terminal_id] = entry
        await self.start_entry(entry)
        return terminal_id

    async def start_entry(self, entry: dict[str, Any]) -> None:
        argv = interactive_command(
            entry["harness"], entry.get("native_session"), entry.get("prompt", "")
        )
        entry["status"] = "starting"
        entry.pop("exit_code", None)
        self.state.save()
        session = TerminalSession(self.state, entry)
        self.sessions[entry["id"]] = session
        try:
            await session.start(argv)
        except BaseException:
            entry["status"] = "failed"
            self.state.save()
            raise

    async def restart(self, terminal_id: str) -> None:
        entry = self.entries[terminal_id]
        if entry.get("status") in ("starting", "running", "recovery_blocked"):
            raise ValueError("Stop the session before restarting it")
        await self.start_entry(entry)

    def snapshot(self) -> dict[str, Any]:
        return {
            key: {**entry, "attached": key in self.sessions}
            for key, entry in self.entries.items()
        }

    def get(self, terminal_id: str) -> TerminalSession:
        if terminal_id not in self.sessions:
            raise ValueError(
                "Terminal is not attached to this supervisor; start a new session"
            )
        return self.sessions[terminal_id]

    async def stop(self, owner: str | None = None) -> None:
        await asyncio.gather(
            *(
                s.stop()
                for s in list(self.sessions.values())
                if owner is None or s.entry["owner"] == owner
            )
        )

    async def stop_ticket(self, ticket_id: str) -> None:
        await asyncio.gather(
            *(
                s.stop()
                for s in list(self.sessions.values())
                if s.entry.get("owner") == "board"
                and s.entry.get("ticket") == ticket_id
            )
        )

    async def reconcile(self, tickets) -> None:
        columns = {ticket.id: ticket.column for ticket in tickets}
        await asyncio.gather(
            *(
                s.stop()
                for s in list(self.sessions.values())
                if s.entry.get("owner") == "board"
                and s.entry.get("ticket")
                and columns.get(s.entry["ticket"]) != s.entry.get("column")
            )
        )

    def running(self, owner: str | None = None) -> bool:
        return any(
            e.get("status") in ("starting", "running", "recovery_blocked")
            and (owner is None or e.get("owner") == owner)
            for e in self.entries.values()
        )

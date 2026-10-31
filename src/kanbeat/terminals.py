from __future__ import annotations

import asyncio
import codecs
import fcntl
import os
import pty
import signal
import struct
import sys
import termios
from typing import Any
import uuid

from .terminal_history import HistoryBoard

from .launcher import internal_environment, pty_child_command
from .native import (
    process_directory,
    process_identity,
    signal_owned_process,
    terminate_process,
)
from .native_settings import NativeSettings, SettingsError, cli_args
from .state import LocalState


def framed_prompt(prompt: str | None) -> str:
    return "Kanbeat interactive instructions:\n" + prompt if prompt else ""


SHELL_HARNESS = "shell"
CWD_POLL_SECONDS = 1.0 if sys.platform.startswith("linux") else 3.0


def interactive_command(
    harness: str,
    session: str | None = None,
    prompt: str = "",
    settings: NativeSettings | None = None,
) -> list[str]:
    if session and (
        session.startswith("-")
        or len(session) > 512
        or any(c.isspace() for c in session)
    ):
        raise ValueError("Invalid native session identifier")
    if harness == SHELL_HARNESS:
        return [os.environ.get("SHELL") or "/bin/sh"]
    prompt = framed_prompt(prompt)
    extra = cli_args(harness, settings, interactive=True)
    if harness == "codex":
        return (
            ["codex", *extra, "resume", session] + ([prompt] if prompt else [])
            if session
            else ["codex", *extra] + ([prompt] if prompt else [])
        )
    if harness == "claude":
        return (
            ["claude", *extra]
            + (["--resume", session] if session else [])
            + ([prompt] if prompt else [])
        )
    if harness == "opencode":
        return (
            ["opencode", *extra]
            + (["--session", session] if session else [])
            + (["--prompt", prompt] if prompt else [])
        )
    if harness == "pi":
        return (
            ["pi", *extra]
            + (["--session", session] if session else [])
            + ([prompt] if prompt else [])
        )
    raise ValueError("Unsupported interactive harness")


class TerminalSession:
    def __init__(self, state: LocalState, entry: dict[str, Any]):
        self.state = state
        self.entry = entry
        self.id = entry["id"]
        self.board = HistoryBoard(
            width=entry.get("cols", 80), height=entry.get("rows", 24)
        )
        self.board.host.attach(self)
        self.process = None
        self.cwd_task: asyncio.Task | None = None
        self.fd: int | None = None
        self.waiter = None
        self.pending = bytearray()
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.log = None
        self.stop_requested = False
        self.pending_stops = 0
        self.revision = 0
        self.changed = asyncio.Event()

    def touch(self) -> None:
        self.revision += 1
        self.changed.set()
        self.changed = asyncio.Event()

    async def wait_for_change(self, revision: int, timeout: float) -> None:
        if self.revision != revision:
            return
        try:
            await asyncio.wait_for(self.changed.wait(), timeout)
        except asyncio.TimeoutError:
            pass

    async def start(self, argv: list[str], env: dict[str, str] | None = None) -> None:
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
                *pty_child_command(argv),
                cwd=self.start_directory(),
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
                env={**internal_environment(env), "TERM": "xterm-256color"},
            )
            self.entry.update(
                status="running",
                pid=self.process.pid,
                process_identity=process_identity(self.process.pid),
            )
            asyncio.get_running_loop().add_reader(master, self.read)
            self.waiter = asyncio.create_task(self.wait())
            if self.entry["harness"] == SHELL_HARNESS:
                self.cwd_task = asyncio.create_task(self.track_directory())
            self.state.save()
        except BaseException:
            await self.stop()
            self.close()
            raise
        finally:
            os.close(slave)

    def start_directory(self):
        saved = self.entry.get("cwd")
        if (
            self.entry["harness"] == SHELL_HARNESS
            and isinstance(saved, str)
            and os.path.isdir(saved)
        ):
            return saved
        return self.state.board.project

    def record_directory(self, directory: str | None) -> None:
        if directory and directory != self.entry.get("cwd"):
            self.entry["cwd"] = directory
            self.state.save()

    async def remember_directory(self) -> None:
        if self.process is not None and self.process.returncode is None:
            self.record_directory(
                await asyncio.to_thread(process_directory, self.process.pid)
            )

    async def track_directory(self) -> None:
        while self.process is not None and self.process.returncode is None:
            await self.remember_directory()
            await asyncio.sleep(CWD_POLL_SECONDS)

    def write(self, data: str) -> None:
        self.write_bytes(data.encode("utf-8"))

    def write_bytes(self, payload: bytes) -> None:
        if self.fd is not None and not self.queue_bytes(payload):
            self.entry["dropped_replies"] = self.entry.get("dropped_replies", 0) + 1
            self.touch()

    def queue_bytes(self, payload: bytes) -> bool:
        if self.fd is None:
            raise ValueError("Terminal input is closed")
        if len(self.pending) + len(payload) > 65536:
            return False
        self.pending.extend(payload)
        self.flush()
        return True

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
        self.touch()

    def resize(self, rows: int, cols: int) -> None:
        if not 1 <= rows <= 100 or not 1 <= cols <= 240:
            raise ValueError("Terminal size must be 1..100 rows and 1..240 columns")
        self.entry.update(rows=rows, cols=cols)
        self.board.resize(cols, rows)
        self.touch()
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
            if self.cwd_task is not None:
                self.cwd_task.cancel()
            self.close()
            self.entry.pop("pid", None)
            self.entry.pop("process_identity", None)
            self.state.save()
            self.touch()

    async def stop(self) -> None:
        self.stop_requested = True
        stopping = self.process is not None and self.process.returncode is None
        if stopping and self.entry["harness"] == SHELL_HARNESS:
            await self.remember_directory()
        if stopping:
            self.pending_stops += 1
            self.entry["status"] = "stopping"
            self.state.save()
            self.touch()
        try:
            if stopping:
                if self.entry["harness"] == SHELL_HARNESS:
                    signal_owned_process(self.process, signal.SIGHUP)
                    try:
                        await asyncio.wait_for(self.process.wait(), timeout=3)
                    except asyncio.TimeoutError:
                        pass
                await terminate_process(self.process)
        except BaseException:
            if self.process.returncode is None and self.pending_stops == 1:
                self.entry["status"] = "running"
                self.stop_requested = False
                self.state.save()
                self.touch()
            raise
        finally:
            if stopping:
                self.pending_stops -= 1
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

    def snapshot(self, history_position: int | None = None) -> dict[str, Any]:
        if history_position is not None and type(history_position) is not int:
            raise ValueError("History position must be an integer")
        screen = self.board.capture_pane()
        position = self.board.history_total
        historical = history_position is not None and history_position < position
        if historical:
            position, screen = self.board.history_window(history_position)
        return {
            **self.entry,
            "revision": self.revision,
            "screen": screen,
            "history_start": self.board.history_start,
            "history_total": self.board.history_total,
            "scroll_position": position,
            "historical": historical,
            "mouse_tracking": self.board.modes.mouse_protocol.name != "OFF",
            "alternate_screen": self.board.blitter.in_alt_screen,
            "alternate_scroll": self.board.modes.alternate_scroll_mode,
            "cursor": [self.board.cursor.x, self.board.cursor.y],
            "cursor_visible": self.board.modes.cursor_visible and not historical,
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
        role: str = "",
        argv: list[str] | None = None,
        env: dict[str, str] | None = None,
        settings: NativeSettings | None = None,
    ) -> str:
        if native_session:
            settings = None
        elif settings is None:
            settings = self.stored_settings(
                {"harness": harness, "native_session": native_session}
            )
        default_argv = interactive_command(harness, native_session, prompt, settings)
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
        if role:
            entry["role"] = role
        if settings is not None and not settings.empty:
            entry["native_settings"] = settings.to_dict()
        self.entries[terminal_id] = entry
        await self.start_entry(entry, argv=argv or default_argv, env=env)
        return terminal_id

    async def start_entry(
        self,
        entry: dict[str, Any],
        *,
        argv: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        if argv is None:
            settings = self.stored_settings(entry)
            argv = interactive_command(
                entry["harness"],
                entry.get("native_session"),
                entry.get("prompt", ""),
                settings,
            )
        entry["status"] = "starting"
        entry.pop("exit_code", None)
        entry.pop("dropped_replies", None)
        self.state.save()
        session = TerminalSession(self.state, entry)
        self.sessions[entry["id"]] = session
        try:
            await session.start(argv, env)
        except BaseException:
            entry["status"] = "failed"
            self.sessions.pop(entry["id"], None)
            self.state.save()
            raise

    def stored_settings(self, entry: dict[str, Any]) -> NativeSettings | None:
        if entry.get("native_session") or entry.get("harness") == SHELL_HARNESS:
            return None
        try:
            return NativeSettings.from_dict(
                entry["harness"],
                self.state.data.get("native_settings", {}).get(entry["harness"]),
            )
        except (SettingsError, KeyError):
            return None

    async def restart(
        self,
        terminal_id: str,
        *,
        argv: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        entry = self.entries[terminal_id]
        if entry.get("owner") == "attach" and argv is None:
            raise ValueError("Reattach the live session from its ticket or slot")
        if entry.get("status") in (
            "starting",
            "running",
            "stopping",
            "recovery_blocked",
        ):
            raise ValueError("Stop the session before restarting it")
        await self.start_entry(entry, argv=argv, env=env)

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
            e.get("status") in ("starting", "running", "stopping", "recovery_blocked")
            and (owner is None or e.get("owner") == owner)
            for e in self.entries.values()
        )

from __future__ import annotations

import asyncio
import math
import time
from typing import Any, TYPE_CHECKING
import uuid

from .claude_runtime import ClaudeRuntimeError, ClaudeSessionMissing
from .codex_host import CodexHostError, CodexRpcError, thread_missing
from .host_process import HostError
from .native import process_identity
from .opencode_host import OpenCodeHttpError

if TYPE_CHECKING:
    from .supervisor import Supervisor
    from .terminals import TerminalSession

MAX_WAIT_SECONDS = 8.0
MAX_INPUT_EVENTS = 256
PASTE_BEGIN = b"\x1b[200~"
PASTE_END = b"\x1b[201~"
PASTE_STALE_SECONDS = 60.0
MAX_PASTES_PER_TERMINAL = 8


async def terminal_request(
    supervisor: Supervisor, request: dict[str, Any]
) -> dict[str, Any]:
    op = request.get("op")
    if op == "terminal_open":
        if supervisor.runtime.data["execution"] != "stopped":
            raise ValueError(
                "Stop the board before opening an interactive task terminal"
            )
        ticket = supervisor.board.find(str(request["ticket"]))
        slot = str(request.get("slot") or "developer")
        state = supervisor.ticket_state(ticket.id)
        assignment = (
            state["developer"]
            if slot == "developer"
            else next((row for row in state["reviewers"] if row["id"] == slot), None)
        )
        if assignment is None:
            raise ValueError("Unknown worker slot")
        for terminal_id, entry in supervisor.terminals.entries.items():
            if (
                entry.get("ticket") == ticket.id
                and entry.get("slot") == slot
                and entry.get("status") in ("starting", "running")
            ):
                return {"terminal": terminal_id}
            if (
                entry.get("ticket") == ticket.id
                and entry.get("slot") == slot
                and entry.get("status") == "stopping"
            ):
                raise ValueError(
                    "Terminal is still stopping; wait for it to exit before reopening"
                )
        harness, session = supervisor.selected_harness(assignment)
        terminal_id = await supervisor.terminals.create(
            harness,
            ticket=ticket.id,
            column=ticket.column,
            slot=slot,
            native_session=session,
            title=f"#{ticket.id} / {slot}",
        )
        return {"terminal": terminal_id}
    if op == "terminal_attach":
        ticket = supervisor.board.find(str(request["ticket"]))
        slot = str(request.get("slot") or "developer")
        state = supervisor.ticket_state(ticket.id)
        assignment = (
            state["developer"]
            if slot == "developer"
            else next((row for row in state["reviewers"] if row["id"] == slot), None)
        )
        if assignment is None:
            raise ValueError("Unknown worker slot")
        runtime = (state.get("runtime") or {}).get(slot) or {}
        harness = str(
            runtime.get("harness")
            or assignment.get("harness")
            or supervisor.runtime.config["default_harness"]
        )
        session = assignment.get("session")
        if harness not in ("opencode", "codex", "claude"):
            raise ValueError(
                "Live attach is available for OpenCode, Codex and Claude sessions; "
                "stop the board to open this slot's task terminal"
            )
        if not isinstance(session, str) or not session:
            raise ValueError(
                "The " + harness.title() + " session has not been reported yet"
            )
        argv: list[str] | None = None
        env: dict[str, str] | None = None
        if harness == "opencode":
            try:
                host = await supervisor.opencode_host()
                await host.read_session(session)
            except OpenCodeHttpError as error:
                if error.status == 404:
                    assignment.pop("session", None)
                    supervisor.save()
                    raise ValueError(
                        "The OpenCode session is no longer available; start the worker again"
                    ) from None
                raise ValueError(str(error)) from error
            except (HostError, OSError) as error:
                raise ValueError(str(error)) from error
            argv = host.viewer_argv(session)
            env = host.child_env()
        elif harness == "codex":
            try:
                host = await supervisor.codex_host()
                await host.observe_thread(session)
            except CodexRpcError as error:
                if thread_missing(error):
                    if supervisor.slot_active(ticket.id, slot):
                        raise ValueError(
                            "The Codex thread is still starting; try again after "
                            "the first turn begins"
                        ) from None
                    assignment.pop("session", None)
                    supervisor.save()
                    raise ValueError(
                        "The Codex thread is no longer available; start the worker again"
                    ) from None
                raise ValueError(str(error)) from error
            except (CodexHostError, HostError, OSError) as error:
                raise ValueError(str(error)) from error
            argv = host.viewer_argv(session)
            env = host.child_env()
        else:
            try:
                argv = await supervisor.claude.attach_argv(
                    session, "attach-" + uuid.uuid4().hex[:20]
                )
            except ClaudeSessionMissing as error:
                if supervisor.slot_active(ticket.id, slot):
                    raise ValueError(
                        "The Claude session is still starting; try again"
                    ) from None
                assignment.pop("session", None)
                supervisor.save()
                raise ValueError(
                    "The Claude session is no longer available; start the worker again"
                ) from error
            except (ClaudeRuntimeError, OSError) as error:
                raise ValueError(str(error)) from error
            env = supervisor.claude.child_env()
        assert argv is not None and env is not None
        for terminal_id, entry in supervisor.terminals.entries.items():
            if (
                entry.get("owner") == "attach"
                and entry.get("ticket") == ticket.id
                and entry.get("slot") == slot
                and entry.get("native_session") == session
            ):
                attached = terminal_id in supervisor.terminals.sessions
                status = entry.get("status")
                if attached and status == "stopping":
                    raise ValueError(
                        "Terminal is still stopping; wait for it to exit before reopening"
                    )
                if attached and status in ("starting", "running"):
                    return {"terminal": terminal_id}
                if not attached and status in (
                    "starting",
                    "running",
                    "stopping",
                    "recovery_blocked",
                ):
                    if status == "recovery_blocked":
                        pid = entry.get("pid")
                        current = process_identity(int(pid)) if pid else None
                        saved = entry.get("process_identity")
                        if current is not None and (not saved or current == saved):
                            raise ValueError(
                                "An orphaned attach process is still running; "
                                "resolve it before reopening"
                            )
                    entry["status"] = "interrupted"
                    entry.pop("pid", None)
                    entry.pop("process_identity", None)
                    supervisor.save()
                await supervisor.terminals.restart(
                    terminal_id,
                    argv=argv,
                    env=env,
                )
                return {"terminal": terminal_id}
        terminal_id = await supervisor.terminals.create(
            harness,
            ticket=ticket.id,
            slot=slot,
            owner="attach",
            native_session=session,
            title=f"#{ticket.id} / {slot} · live",
            argv=argv,
            env=env,
        )
        return {"terminal": terminal_id}
    if op == "terminal_navigate":
        terminal = supervisor.terminals.get(str(request["terminal"]))
        if terminal.entry["status"] != "running":
            raise ValueError("Terminal is no longer running")
        if not monitor_only(supervisor, terminal):
            raise ValueError(
                "Terminal navigation is only available for live worker monitors"
            )
        direction = request.get("direction")
        if direction not in ("up", "down"):
            raise ValueError("Unknown terminal scroll direction")
        try:
            x = min(max(int(request.get("x", 1)), 1), terminal.board.width)
            y = min(max(int(request.get("y", 1)), 1), terminal.board.height)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("Terminal mouse coordinates must be numeric") from exc
        modifiers = request.get("modifiers") or []
        if not isinstance(modifiers, list) or any(
            value not in ("shift", "meta", "ctrl") for value in modifiers
        ):
            raise ValueError("Unsupported mouse modifier")
        navigation = {
            "kind": "wheel",
            "direction": direction,
            "x": x,
            "y": y,
            "modifiers": modifiers,
        }
        if not (
            terminal.board.modes.mouse_protocol.name != "OFF"
            or terminal.board.modes.alternate_scroll_mode
        ):
            if not terminal.board.blitter.in_alt_screen:
                raise ValueError("This monitor has no scrollable view")
            navigation = {
                "kind": "key",
                "key": "pageup" if direction == "up" else "pagedown",
            }
        collector = InputCapture()
        connection = terminal.board.host.connection
        try:
            terminal.board.host.attach(collector)
            apply_input(terminal, navigation)
        finally:
            terminal.board.host.attach(connection)
        if not terminal.queue_bytes(collector.data):
            raise ValueError("Terminal input queue is full")
        return monitor_snapshot(supervisor, terminal, terminal.snapshot())
    if op == "terminal_paste":
        terminal = supervisor.terminals.get(str(request["terminal"]))
        if terminal.entry["status"] != "running":
            supervisor.pastes.discard_terminal(terminal.id)
            raise ValueError("Terminal is no longer running")
        if monitor_only(supervisor, terminal):
            supervisor.pastes.discard_terminal(terminal.id)
            raise ValueError(
                "This is a live worker monitor; wait for the turn or use Board Stop "
                "before sending input"
            )
        prune_pastes(supervisor, time.monotonic())
        phase = request.get("phase")
        paste_id = request.get("paste")
        if not isinstance(paste_id, str) or not paste_id or len(paste_id) > 128:
            raise ValueError("Invalid paste identifier")
        client = request.get("client")
        if client is not None and not isinstance(client, str):
            raise ValueError("Invalid paste client")
        now = time.monotonic()
        if phase == "begin":
            accepted = supervisor.pastes.begin(terminal, paste_id, client, now)
        elif phase == "chunk":
            text = request.get("text")
            if not isinstance(text, str):
                raise ValueError("Paste must be text")
            accepted = supervisor.pastes.chunk(
                terminal, paste_id, text, client, now
            )
        elif phase == "end":
            accepted = supervisor.pastes.end(terminal, paste_id, client, now)
        elif phase == "abort":
            supervisor.pastes.abort(terminal, paste_id)
            return {"accepted": True}
        else:
            raise ValueError("Unknown paste phase")
        return {"accepted": accepted}
    if op in (
        "terminal_snapshot",
        "terminal_wait",
        "terminal_input",
        "terminal_resize",
        "terminal_stop",
    ):
        terminal = supervisor.terminals.get(str(request["terminal"]))
        if op == "terminal_snapshot":
            return monitor_snapshot(
                supervisor, terminal, terminal.snapshot(request.get("history_position"))
            )
        if op == "terminal_wait":
            try:
                revision = int(request.get("revision"))
                timeout = float(request.get("timeout", 5))
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(
                    "Terminal revision and timeout must be numeric"
                ) from exc
            if not math.isfinite(timeout):
                raise ValueError("Terminal wait timeout must be finite")
            timeout = min(max(timeout, 0), MAX_WAIT_SECONDS)
            wait_screen = asyncio.create_task(
                terminal.wait_for_change(revision, timeout)
            )
            wait_shutdown = asyncio.create_task(supervisor.shutdown_requested.wait())
            try:
                await asyncio.wait(
                    {wait_screen, wait_shutdown}, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                for task in (wait_screen, wait_shutdown):
                    task.cancel()
                await asyncio.gather(wait_screen, wait_shutdown, return_exceptions=True)
            return monitor_snapshot(
                supervisor, terminal, terminal.snapshot(request.get("history_position"))
            )
        if op == "terminal_stop":
            if request.get("confirmed") is not True:
                raise ValueError("Explicit terminal stop confirmation required")
            supervisor.pastes.discard_terminal(terminal.id)
            await terminal.stop()
        elif op == "terminal_resize":
            terminal.resize(int(request["rows"]), int(request["cols"]))
        elif terminal.entry["status"] != "running":
            raise ValueError("Terminal is no longer running")
        elif monitor_only(supervisor, terminal):
            raise ValueError(
                "This is a live worker monitor; wait for the turn or use Board Stop "
                "before sending input"
            )
        else:
            events = request.get("events")
            if events is None:
                events = [request]
            if not isinstance(events, list) or len(events) > MAX_INPUT_EVENTS:
                raise ValueError("Too many terminal input events")
            if not all(isinstance(event, dict) for event in events):
                raise ValueError("Terminal input events must be objects")
            collector = InputCapture()
            rejected = []
            connection = terminal.board.host.connection
            try:
                for index, event in enumerate(events):
                    encoded = InputCapture()
                    terminal.board.host.attach(encoded)
                    try:
                        apply_input(terminal, event)
                        collector.write_bytes(encoded.data)
                    except (ValueError, TypeError, OverflowError) as exc:
                        rejected.append({"index": index, "error": str(exc)})
            finally:
                terminal.board.host.attach(connection)
            if rejected:
                if "events" in request:
                    return {
                        "accepted": False,
                        "rejected": rejected,
                        "revision": terminal.revision,
                    }
                raise ValueError(rejected[0]["error"])
            accepted = terminal.queue_bytes(collector.data)
            if "events" in request:
                return {"accepted": accepted, "revision": terminal.revision}
            if not accepted:
                raise ValueError("Terminal input queue is full")
        return monitor_snapshot(supervisor, terminal, terminal.snapshot())
    raise ValueError("Unsupported terminal command")


def monitor_only(supervisor: Supervisor, terminal: TerminalSession) -> bool:
    entry = terminal.entry
    if entry.get("owner") != "attach":
        return False
    ticket, slot = entry.get("ticket"), entry.get("slot")
    return bool(ticket and slot and supervisor.slot_active(ticket, slot))


def monitor_snapshot(
    supervisor: Supervisor, terminal: TerminalSession, snapshot: dict[str, Any]
) -> dict[str, Any]:
    snapshot["read_only"] = monitor_only(supervisor, terminal)
    return snapshot


class InputCapture:
    def __init__(self):
        self.data = bytearray()

    def write(self, text: str) -> None:
        self.write_bytes(text.encode("utf-8"))

    def write_bytes(self, payload: bytes) -> None:
        if len(self.data) + len(payload) > 65536:
            raise ValueError("Terminal input batch is too large")
        self.data.extend(payload)


class PasteAssembler:
    def __init__(self) -> None:
        self.terminals: dict[str, dict[str, dict[str, Any]]] = {}

    def begin(
        self,
        terminal: TerminalSession,
        paste_id: str,
        client: str | None,
        now: float,
    ) -> bool:
        pastes = self.terminals.setdefault(terminal.id, {})
        for existing, state in list(pastes.items()):
            if client and state.get("client") == client:
                self._close(terminal, state)
                pastes.pop(existing, None)
        if paste_id in pastes:
            raise ValueError("Paste is already in progress")
        while len(pastes) >= MAX_PASTES_PER_TERMINAL:
            oldest = min(pastes, key=lambda key: pastes[key]["updated"])
            self._close(terminal, pastes.pop(oldest))
        bracketed = bool(terminal.board.modes.bracketed_paste)
        if bracketed and not terminal.queue_bytes(PASTE_BEGIN):
            if not pastes:
                self.terminals.pop(terminal.id, None)
            return False
        pastes[paste_id] = {
            "bracketed": bracketed,
            "updated": now,
            "client": client,
        }
        return True

    def chunk(
        self,
        terminal: TerminalSession,
        paste_id: str,
        text: str,
        client: str | None,
        now: float,
    ) -> bool:
        state = self._state(terminal.id, paste_id, client)
        if not terminal.queue_bytes(text.encode("utf-8")):
            return False
        state["updated"] = now
        return True

    def end(
        self,
        terminal: TerminalSession,
        paste_id: str,
        client: str | None,
        now: float,
    ) -> bool:
        state = self._state(terminal.id, paste_id, client)
        if state["bracketed"] and not terminal.queue_bytes(PASTE_END):
            return False
        self._discard(terminal.id, paste_id)
        return True

    def abort(self, terminal: TerminalSession, paste_id: str) -> None:
        pastes = self.terminals.get(terminal.id)
        if not pastes:
            return
        state = pastes.get(paste_id)
        if state is not None:
            self._close(terminal, state)
        self._discard(terminal.id, paste_id)

    def discard_terminal(self, terminal_id: str) -> None:
        self.terminals.pop(terminal_id, None)

    def prune(
        self, now: float, active_ids: set[str] | None = None
    ) -> list[tuple[str, bool]]:
        expired: list[tuple[str, bool]] = []
        for terminal_id in list(self.terminals):
            if active_ids is not None and terminal_id not in active_ids:
                self.terminals.pop(terminal_id, None)
                continue
            pastes = self.terminals[terminal_id]
            for paste_id in list(pastes):
                state = pastes[paste_id]
                if now - state["updated"] > PASTE_STALE_SECONDS:
                    expired.append((terminal_id, bool(state["bracketed"])))
                    del pastes[paste_id]
            if not pastes:
                self.terminals.pop(terminal_id, None)
        return expired

    def _state(
        self, terminal_id: str, paste_id: str, client: str | None
    ) -> dict[str, Any]:
        state = (self.terminals.get(terminal_id) or {}).get(paste_id)
        if state is None:
            raise ValueError("Unknown paste")
        owner = state.get("client")
        if client and owner and client != owner:
            raise ValueError("Paste belongs to another client")
        return state

    def _discard(self, terminal_id: str, paste_id: str) -> None:
        pastes = self.terminals.get(terminal_id)
        if pastes is None:
            return
        pastes.pop(paste_id, None)
        if not pastes:
            self.terminals.pop(terminal_id, None)

    @staticmethod
    def _close(terminal: TerminalSession, state: dict[str, Any]) -> None:
        if not state["bracketed"]:
            return
        try:
            terminal.queue_bytes(PASTE_END)
        except (ValueError, OSError):
            pass


def prune_pastes(
    supervisor: Supervisor, now: float, active_ids: set[str] | None = None
) -> None:
    for terminal_id, bracketed in supervisor.pastes.prune(now, active_ids):
        if not bracketed:
            continue
        session = supervisor.terminals.sessions.get(terminal_id)
        if session is None:
            continue
        try:
            session.queue_bytes(PASTE_END)
        except (ValueError, OSError):
            pass


def apply_input(terminal: TerminalSession, request: dict[str, Any]) -> None:
    if request.get("kind") == "paste":
        text = request.get("text") or ""
        if not isinstance(text, str):
            raise ValueError("Paste must be text")
        terminal.board.input_paste(text)
        return
    from bittty import constants

    if request.get("kind") == "wheel":
        direction = request.get("direction")
        if direction not in ("up", "down"):
            raise ValueError("Unknown terminal scroll direction")
        x = min(max(int(request.get("x", 1)), 1), terminal.board.width)
        y = min(max(int(request.get("y", 1)), 1), terminal.board.height)
        modifiers = request.get("modifiers") or []
        if not isinstance(modifiers, list) or any(
            value not in ("shift", "meta", "ctrl") for value in modifiers
        ):
            raise ValueError("Unsupported mouse modifier")
        button = (
            constants.MOUSE_BUTTON_WHEEL_UP
            if direction == "up"
            else constants.MOUSE_BUTTON_WHEEL_DOWN
        )
        terminal.board.input_mouse(x, y, button, "press", set(modifiers))
        return

    key = str(request.get("key") or "")
    parts = key.split("+")
    modifiers, base = parts[:-1], parts[-1]
    if any(m not in ("ctrl", "alt", "shift", "meta") for m in modifiers):
        raise ValueError("Unsupported key modifier")
    modifier = (
        1
        + ("shift" in modifiers)
        + 2 * ("alt" in modifiers or "meta" in modifiers)
        + 4 * ("ctrl" in modifiers)
    )
    named = {
        "enter": "\r",
        "tab": "\t",
        "escape": "\x1b",
        "backspace": constants.BS,
        "space": " ",
    }
    if base.startswith("f") and base[1:].isdigit():
        terminal.board.input_fkey(int(base[1:]), modifier)
    elif modifiers == ["ctrl"] and base == "enter":
        terminal.board.input_key(constants.LF)
    else:
        character = request.get("character")
        if modifiers == ["ctrl"] and len(base) == 1 and "a" <= base <= "z":
            character = base
        terminal.board.input_key(
            character
            if isinstance(character, str) and character
            else named.get(base, base),
            modifier,
        )

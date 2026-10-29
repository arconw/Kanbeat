from __future__ import annotations

import asyncio
import math
from typing import Any, TYPE_CHECKING

from .host_process import HostError
from .native import process_identity
from .opencode_host import OpenCodeHttpError

if TYPE_CHECKING:
    from .supervisor import Supervisor
    from .terminals import TerminalSession

MAX_WAIT_SECONDS = 8.0
MAX_INPUT_EVENTS = 256


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
            prompt=supervisor.prompt(ticket, slot, bool(session)),
            title=f"#{ticket.id} / {slot}",
        )
        state.setdefault("runtime", {}).pop(slot, None)
        state.setdefault("review_results", {}).pop(slot, None)
        supervisor.save()
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
        if harness != "opencode":
            raise ValueError(
                "Live attach is only available for OpenCode sessions; "
                "stop the board to open this slot's task terminal"
            )
        if not isinstance(session, str) or not session:
            raise ValueError("The OpenCode session has not been reported yet")
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
                    argv=host.viewer_argv(session),
                    env=host.child_env(),
                )
                return {"terminal": terminal_id}
        terminal_id = await supervisor.terminals.create(
            harness,
            ticket=ticket.id,
            slot=slot,
            owner="attach",
            native_session=session,
            title=f"#{ticket.id} / {slot} · live",
            argv=host.viewer_argv(session),
            env=host.child_env(),
        )
        return {"terminal": terminal_id}
    if op in (
        "terminal_snapshot",
        "terminal_wait",
        "terminal_input",
        "terminal_resize",
        "terminal_stop",
    ):
        terminal = supervisor.terminals.get(str(request["terminal"]))
        if op == "terminal_snapshot":
            return terminal.snapshot(request.get("history_position"))
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
            return terminal.snapshot(request.get("history_position"))
        if op == "terminal_stop":
            if request.get("confirmed") is not True:
                raise ValueError("Explicit terminal stop confirmation required")
            await terminal.stop()
        elif op == "terminal_resize":
            terminal.resize(int(request["rows"]), int(request["cols"]))
        elif terminal.entry["status"] != "running":
            raise ValueError("Terminal is no longer running")
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
        return terminal.snapshot()
    raise ValueError("Unsupported terminal command")


class InputCapture:
    def __init__(self):
        self.data = bytearray()

    def write(self, text: str) -> None:
        self.write_bytes(text.encode("utf-8"))

    def write_bytes(self, payload: bytes) -> None:
        if len(self.data) + len(payload) > 65536:
            raise ValueError("Terminal input batch is too large")
        self.data.extend(payload)


def apply_input(terminal: TerminalSession, request: dict[str, Any]) -> None:
    if request.get("kind") == "paste":
        text = request.get("text") or ""
        if not isinstance(text, str):
            raise ValueError("Paste must be text")
        if len(text.encode()) > 16000:
            raise ValueError("Paste is too large")
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
    else:
        character = request.get("character")
        terminal.board.input_key(
            character
            if isinstance(character, str) and character
            else named.get(base, base),
            modifier,
        )

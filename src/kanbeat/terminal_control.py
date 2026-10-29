from __future__ import annotations

import math
from typing import Any, TYPE_CHECKING

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
            timeout = float(request.get("timeout", 5))
            if not math.isfinite(timeout):
                raise ValueError("Terminal wait timeout must be finite")
            timeout = min(max(timeout, 0), MAX_WAIT_SECONDS)
            await terminal.wait_for_change(int(request["revision"]), timeout)
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
            connection = terminal.board.host.connection
            terminal.board.host.attach(collector)
            try:
                for event in events:
                    apply_input(terminal, event)
            finally:
                terminal.board.host.attach(connection)
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
        text = str(request.get("text") or "")
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

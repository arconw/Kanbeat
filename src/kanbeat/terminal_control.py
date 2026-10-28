from __future__ import annotations

from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .supervisor import Supervisor


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
        "terminal_input",
        "terminal_resize",
        "terminal_stop",
    ):
        terminal = supervisor.terminals.get(str(request["terminal"]))
        if op == "terminal_snapshot":
            return terminal.snapshot()
        if op == "terminal_stop":
            if request.get("confirmed") is not True:
                raise ValueError("Explicit terminal stop confirmation required")
            await terminal.stop()
        elif op == "terminal_resize":
            terminal.resize(int(request["rows"]), int(request["cols"]))
        elif terminal.entry["status"] != "running":
            raise ValueError("Terminal is no longer running")
        elif request.get("kind") == "paste":
            text = str(request.get("text") or "")
            if len(text.encode()) > 16000:
                raise ValueError("Paste is too large")
            terminal.board.input_paste(text)
        else:
            from bittty import constants

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
        return terminal.snapshot()
    raise ValueError("Unsupported terminal command")

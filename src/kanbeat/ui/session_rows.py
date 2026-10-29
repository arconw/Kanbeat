from __future__ import annotations

from copy import deepcopy
from rich.markup import escape
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Collapsible, Static


def lifecycle_label(entry):
    """Process lifecycle only; never infer native busy/idle from a PID."""
    status = str(entry.get("status") or "not started")
    code = entry.get("exit_code")
    if code is not None:
        status += f" (exit {code})"
    raw_error = str(entry.get("error") or "")
    printable = "".join(char if char.isprintable() else " " for char in raw_error)
    error = " ".join(printable.split())
    if error:
        status += " / error: " + error[:100]
    return status


class SessionRow(Horizontal):
    def __init__(self, entry):
        super().__init__(id="session-row-" + entry["id"], classes="slot-row")
        self.entry = deepcopy(entry)

    @staticmethod
    def label(entry):
        return f"{entry['title']} / {entry['harness']} / {lifecycle_label(entry)}"

    def compose(self) -> ComposeResult:
        yield Static(self.label(self.entry), classes="slot-name", markup=False)
        yield Button(
            "Open",
            id="open-session-" + self.entry["id"],
            disabled=not self.entry.get("attached"),
        )
        yield Button("Actions", id="session-actions-" + self.entry["id"])

    def update_entry(self, entry):
        if self.label(entry) != self.label(self.entry):
            self.query_one(Static).update(self.label(entry))
        self.query_one("#open-session-" + entry["id"], Button).disabled = not entry.get(
            "attached"
        )
        self.entry = deepcopy(entry)


class TaskSessions(Vertical):
    def __init__(self, ticket, running, terminals=None):
        super().__init__(id="task-sessions-" + ticket["id"])
        self.ticket = ticket
        self.running = running
        self.terminals = terminals or {}

    @staticmethod
    def slots(ticket):
        return ["developer"] + [
            row["id"] for row in (ticket.get("state") or {}).get("reviewers", [])
        ]

    @staticmethod
    def slot_status(ticket, slot, terminals):
        worker = ((ticket.get("state") or {}).get("runtime") or {}).get(slot) or {}
        parts = []
        if worker:
            parts.append("Worker " + lifecycle_label(worker))
        matches = [
            entry
            for entry in terminals.values()
            if entry.get("owner") == "board"
            and entry.get("ticket") == ticket["id"]
            and entry.get("slot") == slot
        ]
        active = next(
            (
                entry
                for entry in reversed(matches)
                if entry.get("status") in ("starting", "running", "recovery_blocked")
            ),
            None,
        )
        terminal = active or (matches[-1] if matches else None)
        if terminal:
            parts.append("PTY " + lifecycle_label(terminal))
        return " / ".join(parts) if parts else "not started"

    def row(self, slot):
        row = Horizontal(
            classes="slot-row", id=f"task-slot-{self.ticket['id']}-{slot}", name=slot
        )
        row.compose_add_child(Static(slot, classes="slot-name"))
        row.compose_add_child(
            Button(
                "Terminal", id=f"term-{self.ticket['id']}-{slot}", disabled=self.running
            )
        )
        row.compose_add_child(Button("Logs", id=f"log-{self.ticket['id']}-{slot}"))
        row.compose_add_child(
            Static(
                self.slot_status(self.ticket, slot, self.terminals),
                classes="task-status",
                id=f"task-status-{self.ticket['id']}-{slot}",
                markup=False,
            )
        )
        return row

    def compose(self) -> ComposeResult:
        yield Static(
            self.ticket["title"] + " · " + self.ticket["id"],
            classes="session-heading",
            markup=False,
        )
        for slot in self.slots(self.ticket):
            yield self.row(slot)

    async def update_ticket(self, ticket, running, terminals=None):
        if ticket["title"] != self.ticket["title"]:
            self.query_one(".session-heading", Static).update(
                ticket["title"] + " · " + ticket["id"]
            )
        self.ticket, self.running, self.terminals = ticket, running, terminals or {}
        wanted = self.slots(ticket)
        rows = {row.name: row for row in self.query(".slot-row")}
        for slot, row in rows.items():
            if slot not in wanted:
                await row.remove()
        for slot in wanted:
            if slot not in rows:
                await self.mount(self.row(slot))
            self.query_one(f"#term-{ticket['id']}-{slot}", Button).disabled = running
            self.query_one(f"#task-status-{ticket['id']}-{slot}", Static).update(
                self.slot_status(ticket, slot, self.terminals)
            )
        if [row.name for row in self.query(".slot-row")] != wanted:
            order = {slot: index for index, slot in enumerate(wanted)}
            self.sort_children(key=lambda row: order.get(row.name, -1))


class SessionSection(Collapsible):
    def __init__(self, key: str, title: str, collapsed: bool):
        super().__init__(
            title=escape(title), collapsed=collapsed, id="section-" + key, name=key
        )

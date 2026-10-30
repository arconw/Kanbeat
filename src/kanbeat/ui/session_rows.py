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


def worker_is_active(entry):
    return entry.get("status") == "running" and entry.get("activity") not in (
        "idle",
        "waiting",
        "error",
    )


def live_session(ticket, slot):
    state = ticket.get("state") or {}
    runtime = (state.get("runtime") or {}).get(slot) or {}
    if runtime.get("harness") not in ("opencode", "codex", "claude"):
        return None
    assignment = (
        state.get("developer")
        if slot == "developer"
        else next(
            (row for row in state.get("reviewers") or [] if row.get("id") == slot),
            None,
        )
    )
    session = (assignment or {}).get("session")
    return session if isinstance(session, str) and session else None


def worker_status_label(entry):
    status = lifecycle_label(entry)
    activity = entry.get("activity")
    if entry.get("status") == "running" and activity in (
        "working",
        "waiting",
        "idle",
        "error",
    ):
        status += " / " + activity
    return status


class SessionRow(Horizontal):
    def __init__(self, entry):
        super().__init__(id="session-row-" + entry["id"], classes="slot-row")
        self.entry = deepcopy(entry)

    @staticmethod
    def label(entry):
        status = (
            worker_status_label(entry)
            if entry.get("activity")
            else lifecycle_label(entry)
        )
        return f"{entry['title']} / {entry['harness']} / {status}"

    def compose(self) -> ComposeResult:
        label = Static(self.label(self.entry), classes="slot-name", markup=False)
        label.tooltip = self.label(self.entry)
        yield label
        yield Button(
            "Open",
            id="open-session-" + self.entry["id"],
            disabled=not self.entry.get("attached")
            or self.entry.get("status") == "stopping",
        )
        yield Button("Actions", id="session-actions-" + self.entry["id"])

    def update_entry(self, entry):
        if self.label(entry) != self.label(self.entry):
            label = self.query_one(Static)
            label.update(self.label(entry))
            label.tooltip = self.label(entry)
        self.query_one("#open-session-" + entry["id"], Button).disabled = (
            not entry.get("attached") or entry.get("status") == "stopping"
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
            parts.append("Worker " + worker_status_label(worker))
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
                if entry.get("status")
                in ("starting", "running", "stopping", "recovery_blocked")
            ),
            None,
        )
        terminal = active or (matches[-1] if matches else None)
        if terminal:
            parts.append("PTY " + lifecycle_label(terminal))
        return " / ".join(parts) if parts else "not started"

    def worker_active(self, slot):
        worker = ((self.ticket.get("state") or {}).get("runtime") or {}).get(slot)
        return bool(self.running and worker and worker_is_active(worker))

    def row(self, slot):
        row = Horizontal(
            classes="slot-row", id=f"task-slot-{self.ticket['id']}-{slot}", name=slot
        )
        row.compose_add_child(Static(slot, classes="slot-name"))
        row.compose_add_child(
            Button(
                "Terminal",
                id=f"term-{self.ticket['id']}-{slot}",
                disabled=self.running and live_session(self.ticket, slot) is None,
            )
        )
        row.compose_add_child(Button("Logs", id=f"log-{self.ticket['id']}-{slot}"))
        status = Static(
            self.slot_status(self.ticket, slot, self.terminals),
            classes="task-status",
            id=f"task-status-{self.ticket['id']}-{slot}",
            markup=False,
        )
        status.tooltip = self.slot_status(self.ticket, slot, self.terminals)
        status.set_class(self.worker_active(slot), "agent-working")
        row.compose_add_child(status)
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
            self.query_one(f"#term-{ticket['id']}-{slot}", Button).disabled = (
                running and live_session(ticket, slot) is None
            )
            status = self.query_one(f"#task-status-{ticket['id']}-{slot}", Static)
            status.update(self.slot_status(ticket, slot, self.terminals))
            status.tooltip = self.slot_status(ticket, slot, self.terminals)
            active = self.worker_active(slot)
            status.set_class(active, "agent-working")
            if not active:
                status.remove_class("agent-pulse")
        if [row.name for row in self.query(".slot-row")] != wanted:
            order = {slot: index for index, slot in enumerate(wanted)}
            self.sort_children(key=lambda row: order.get(row.name, -1))


class SessionSection(Collapsible):
    def __init__(self, key: str, title: str, collapsed: bool):
        super().__init__(
            title=escape(title), collapsed=collapsed, id="section-" + key, name=key
        )

from __future__ import annotations

from copy import deepcopy
from rich.markup import escape
from rich.text import Text
from textual import events
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


STATE_KINDS = {
    "running": "ok",
    "starting": "busy",
    "stopping": "busy",
    "recovering": "busy",
    "interrupted": "warn",
    "exited": "idle",
    "stopped": "idle",
    "failed": "error",
    "recovery_blocked": "error",
}


class SessionRow(Horizontal):
    def __init__(self, entry):
        super().__init__(id="session-row-" + entry["id"], classes="session-row")
        self.entry = deepcopy(entry)

    @staticmethod
    def label(entry):
        status = (
            worker_status_label(entry)
            if entry.get("activity")
            else lifecycle_label(entry)
        )
        return f"{entry['title']} / {entry['harness']} / {status}"

    @staticmethod
    def title(entry):
        return f"{entry['title']} / {entry['harness']}"

    @staticmethod
    def state_kind(entry):
        if entry.get("activity") == "error":
            return "error"
        return STATE_KINDS.get(str(entry.get("status")), "idle")

    @staticmethod
    def openable(entry):
        return entry.get("status") != "stopping"

    def compose(self) -> ComposeResult:
        opener = Button(
            Text(self.title(self.entry)),
            id="open-session-" + self.entry["id"],
            classes="card-title",
            disabled=not self.openable(self.entry),
        )
        opener.tooltip = self.label(self.entry)
        yield opener
        with Vertical(classes="session-side"):
            yield Button(
                "⋯", id="session-actions-" + self.entry["id"], classes="card-menu"
            )
            state = Static(
                "■",
                id="session-state-" + self.entry["id"],
                classes="session-state state-" + self.state_kind(self.entry),
            )
            state.tooltip = lifecycle_label(self.entry)
            yield state

    async def on_click(self, event: events.Click) -> None:
        event.stop()
        if self.openable(self.entry):
            await self.app.resume_session(self.entry["id"])

    def update_entry(self, entry):
        opener = self.query_one("#open-session-" + entry["id"], Button)
        if self.title(entry) != self.title(self.entry):
            opener.label = Text(self.title(entry))
        opener.tooltip = self.label(entry)
        opener.disabled = not self.openable(entry)
        state = self.query_one("#session-state-" + entry["id"], Static)
        for kind in set(STATE_KINDS.values()):
            state.set_class(kind == self.state_kind(entry), "state-" + kind)
        state.tooltip = lifecycle_label(entry)
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
    def __init__(self, key: str, title: str, collapsed: bool, menu_id: str = ""):
        super().__init__(
            title=escape(title), collapsed=collapsed, id="section-" + key, name=key
        )
        self.menu_id = menu_id

    def compose(self) -> ComposeResult:
        with Horizontal(classes="section-head"):
            yield self._title
            if self.menu_id:
                yield Button("...", id=self.menu_id, classes="group-menu")
        with self.Contents():
            yield from self._contents_list

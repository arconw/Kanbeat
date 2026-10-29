from __future__ import annotations

from typing import Any
from copy import deepcopy

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Static

from ..filesystem import COLUMNS


def summary(ticket: dict[str, Any]) -> str:
    return next(
        (
            line.strip()[:100]
            for line in ticket["body"].splitlines()
            if line.strip() and not line.startswith("#")
        ),
        "No description",
    )


def runtime(ticket: dict[str, Any]) -> str:
    slots = (ticket.get("state") or {}).get("runtime") or {}
    return ", ".join(
        f"{slot_label(name)}:{info.get('status', '?')}" for name, info in slots.items()
    )[:88]


def worker_active(ticket: dict[str, Any]) -> bool:
    return any(
        entry.get("status") == "running"
        for entry in ((ticket.get("state") or {}).get("runtime") or {}).values()
    )


def slot_label(name: str) -> str:
    if name == "developer":
        return "dev"
    if name.startswith("reviewer-"):
        return "r" + name[len("reviewer-") :]
    return name[:8]


def card_title(ticket: dict[str, Any]) -> str:
    title = ticket["title"]
    return title[:21] + "…" if len(title) > 22 else title


class TicketCard(Vertical):
    def __init__(self, ticket: dict[str, Any], select_mode: bool, selected: bool):
        super().__init__(id="card-" + ticket["id"], classes="card")
        self.ticket = deepcopy(ticket)
        self.select_mode = select_mode
        self.selected = selected

    def compose(self) -> ComposeResult:
        with Horizontal(classes="card-head"):
            yield Button(
                Text(card_title(self.ticket)),
                id="view-" + self.ticket["id"],
                classes="card-title",
            )
            yield Button("⋯", id="act-" + self.ticket["id"], classes="card-menu")
        yield Static(summary(self.ticket), classes="card-summary", markup=False)
        status = runtime(self.ticket)
        indicator = Static("◌ " + status, classes="card-runtime", markup=False)
        indicator.display = bool(status)
        indicator.set_class(
            self.app.snapshot.get("execution") == "running"
            and worker_active(self.ticket),
            "agent-working",
        )
        yield indicator
        selected = Button(
            ("☑" if self.selected else "☐") + " Select",
            id="pick-" + self.ticket["id"],
            classes="check-button",
        )
        selected.display = self.select_mode
        yield selected

    def update_ticket(
        self, ticket: dict[str, Any], select_mode: bool, selected: bool
    ) -> None:
        if ticket["title"] != self.ticket["title"]:
            self.query_one(".card-title", Button).label = Text(card_title(ticket))
        if summary(ticket) != summary(self.ticket):
            self.query_one(".card-summary", Static).update(summary(ticket))
        status = runtime(ticket)
        indicator = self.query_one(".card-runtime", Static)
        if status != runtime(self.ticket):
            indicator.update("◌ " + status)
            indicator.display = bool(status)
        active = self.app.snapshot.get("execution") == "running" and worker_active(
            ticket
        )
        indicator.set_class(active, "agent-working")
        if not active:
            indicator.remove_class("agent-pulse")
        selected_button = self.query_one(".check-button", Button)
        if selected != self.selected:
            selected_button.label = ("☑" if selected else "☐") + " Select"
        if select_mode != self.select_mode:
            selected_button.display = select_mode
        self.ticket, self.select_mode, self.selected = (
            deepcopy(ticket),
            select_mode,
            selected,
        )


class BoardView(Vertical):
    def __init__(self, snapshot, select_mode: bool, selection: set[str]):
        super().__init__(id="board-page")
        self.snapshot = snapshot
        self.select_mode = select_mode
        self.selection = set(selection)

    def items(self, column: str):
        return [t for t in self.snapshot.get("tickets", []) if t["column"] == column]

    @staticmethod
    def heading(column: str, count: int) -> str:
        title = "REVIEW/QA" if column == "review" else column.upper()
        return f"{title}   {count:02d}"

    def compose(self) -> ComposeResult:
        with Horizontal(classes="toolbar"):
            yield Static("BOARD / PROJECT PIPELINE", classes="page-heading")
            yield Button(
                "Select mode " + ("ON" if self.select_mode else "OFF"), id="select-mode"
            )
            yield Button(
                "Bulk archive", id="bulk-archive", disabled=not self.select_mode
            )
            yield Button("+ Ticket", id="new-ticket")
        with Horizontal(id="lanes"):
            for column in COLUMNS[:-1]:
                items = self.items(column)
                with VerticalScroll(id="lane-" + column, classes="lane"):
                    yield Static(self.heading(column, len(items)), classes="lane-title")
                    for ticket in items:
                        yield TicketCard(
                            ticket, self.select_mode, ticket["id"] in self.selection
                        )

    async def update_snapshot(self, snapshot, select_mode: bool, selection: set[str]):
        if select_mode != self.select_mode:
            self.query_one("#select-mode", Button).label = "Select mode " + (
                "ON" if select_mode else "OFF"
            )
            self.query_one("#bulk-archive", Button).disabled = not select_mode
        counts = {column: len(self.items(column)) for column in COLUMNS[:-1]}
        self.snapshot = snapshot
        self.select_mode, self.selection = select_mode, set(selection)
        wanted_columns = {
            ticket["id"]: ticket["column"] for ticket in snapshot.get("tickets", [])
        }
        for card in list(self.query(TicketCard)):
            if wanted_columns.get(card.ticket["id"]) != card.ticket["column"]:
                await card.remove()
        for column in COLUMNS[:-1]:
            lane = self.query_one("#lane-" + column, VerticalScroll)
            items = self.items(column)
            wanted = {ticket["id"]: ticket for ticket in items}
            cards = {card.ticket["id"]: card for card in lane.query(TicketCard)}
            if counts[column] != len(items):
                lane.query_one(".lane-title", Static).update(
                    self.heading(column, len(items))
                )
            for ticket_id, card in cards.items():
                if ticket_id not in wanted:
                    await card.remove()
            for ticket_id, ticket in wanted.items():
                if ticket_id in cards:
                    cards[ticket_id].update_ticket(
                        ticket, select_mode, ticket_id in selection
                    )
                else:
                    await lane.mount(
                        TicketCard(ticket, select_mode, ticket_id in selection)
                    )
            expected = ["card-" + ticket_id for ticket_id in wanted]
            actual = [card.id for card in lane.query(TicketCard)]
            if actual != expected:
                order = {card_id: index for index, card_id in enumerate(expected)}
                lane.sort_children(key=lambda widget: order.get(widget.id, -1))

from __future__ import annotations

from typing import Any
from copy import deepcopy

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widget import Widget
from textual.widgets import Button, Static

from ..filesystem import COLUMNS, is_blocker_line
from .session_rows import worker_is_active


def summary(ticket: dict[str, Any]) -> str:
    return next(
        (
            line.strip()[:100]
            for line in ticket["body"].splitlines()
            if line.strip() and not line.startswith("#") and not is_blocker_line(line)
        ),
        "No description",
    )


def waiting_links(ticket: dict[str, Any], key: str) -> list[str]:
    return ["#" + row["id"] for row in ticket.get(key, []) if not row.get("satisfied")]


def link_summary(ticket: dict[str, Any]) -> str:
    parts = []
    blockers = waiting_links(ticket, "blocked_by")
    if blockers:
        parts.append("⛔ blocked by " + " ".join(blockers))
    blocked = waiting_links(ticket, "blocks")
    if blocked:
        parts.append("→ blocks " + " ".join(blocked))
    return "\n".join(parts)


SECTIONS = (("BLOCKED BY", "blocked_by"), ("BLOCKS", "blocks"))


def dependency_label(row: dict[str, Any]) -> str:
    return f"#{row['id']}  {row['title'] or '(missing ticket)'}  [{row['column']}]"


def dependency_text(ticket: dict[str, Any]) -> str:
    return "\n\n".join(
        "\n".join([heading, *map(dependency_label, ticket[key])])
        for heading, key in SECTIONS
        if ticket.get(key)
    )


def dependency_widgets(ticket: dict[str, Any]) -> list[Widget]:
    widgets: list[Widget] = []
    for heading, key in SECTIONS:
        rows = ticket.get(key, [])
        if rows:
            widgets.append(Static(heading, classes="dependency-heading"))
            widgets.extend(
                Button(
                    Text(dependency_label(row)),
                    classes="dependency",
                    name=row["id"],
                    disabled=row["column"] == "missing",
                )
                for row in rows
            )
    return widgets


def runtime_status(info: dict[str, Any]) -> str:
    status = str(info.get("status", "?"))
    activity = info.get("activity")
    if status == "running" and activity in ("working", "waiting", "idle", "error"):
        status += "/" + str(activity)
    return status


def runtime(ticket: dict[str, Any]) -> str:
    slots = (ticket.get("state") or {}).get("runtime") or {}
    return ", ".join(
        f"{slot_label(name)}:{runtime_status(info)}" for name, info in slots.items()
    )[:88]


def worker_active(ticket: dict[str, Any]) -> bool:
    return any(
        worker_is_active(entry)
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
    if len(title) > 34:
        title = title[:33] + "…"
    return f"#{ticket['id']} {title}"


LANES = COLUMNS[:-1]


class TicketCard(Vertical):
    def __init__(self, ticket: dict[str, Any], select_mode: bool, selected: bool):
        super().__init__(id="card-" + ticket["id"], classes="card")
        self.ticket = deepcopy(ticket)
        self.select_mode = select_mode
        self.selected = selected

    def compose(self) -> ComposeResult:
        position = LANES.index(self.ticket["column"])
        with Horizontal(classes="card-bar"):
            yield Button("=", id="menu-" + self.ticket["id"], classes="card-step")
            yield Button(
                "<",
                id="left-" + self.ticket["id"],
                classes="card-step",
                disabled=position == 0,
            )
            yield Button(
                ">",
                id="right-" + self.ticket["id"],
                classes="card-step",
                disabled=position == len(LANES) - 1,
            )
        yield Button(
            Text(card_title(self.ticket)),
            id="view-" + self.ticket["id"],
            classes="card-title",
        )
        yield Static(summary(self.ticket), classes="card-summary", markup=False)
        links = Static(link_summary(self.ticket), classes="card-links", markup=False)
        links.display = bool(link_summary(self.ticket))
        yield links
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
        if card_title(ticket) != card_title(self.ticket):
            self.query_one(".card-title", Button).label = Text(card_title(ticket))
        if summary(ticket) != summary(self.ticket):
            self.query_one(".card-summary", Static).update(summary(ticket))
        if link_summary(ticket) != link_summary(self.ticket):
            links = self.query_one(".card-links", Static)
            links.update(link_summary(ticket))
            links.display = bool(link_summary(ticket))
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


LANE_MIN_WIDTH = 30


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
            for column in LANES:
                items = self.items(column)
                with VerticalScroll(id="lane-" + column, classes="lane"):
                    yield Static(self.heading(column, len(items)), classes="lane-title")
                    for ticket in items:
                        yield TicketCard(
                            ticket, self.select_mode, ticket["id"] in self.selection
                        )

    def on_resize(self) -> None:
        self.fit_lanes()

    def fit_lanes(self) -> None:
        lanes = self.query(".lane")
        if not lanes:
            return
        room = self.query_one("#lanes").size.width
        width = max(LANE_MIN_WIDTH, room // len(lanes) - 1)
        for lane in lanes:
            lane.styles.width = width

    async def update_snapshot(self, snapshot, select_mode: bool, selection: set[str]):
        if select_mode != self.select_mode:
            self.query_one("#select-mode", Button).label = "Select mode " + (
                "ON" if select_mode else "OFF"
            )
            self.query_one("#bulk-archive", Button).disabled = not select_mode
        counts = {column: len(self.items(column)) for column in LANES}
        self.snapshot = snapshot
        self.select_mode, self.selection = select_mode, set(selection)
        wanted_columns = {
            ticket["id"]: ticket["column"] for ticket in snapshot.get("tickets", [])
        }
        for card in list(self.query(TicketCard)):
            if wanted_columns.get(card.ticket["id"]) != card.ticket["column"]:
                await card.remove()
        for column in LANES:
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

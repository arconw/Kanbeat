"""Kanbeat TUI. A mouse-clickable view/controller for the supervisor, never its owner."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from .modal import BackdropModal
from textual.widgets import Button, Input, Markdown, Select, Static, TextArea

from ..supervisor import request
from .terminal import TerminalView
from .sessions import SessionsView
from .options import HARNESS
from .board import BoardView
from .preferences import ViewPreferences
from .settings import Settings
from .startup import StartupScreen

COLUMNS = ("backlog", "todo", "progress", "review", "done", "failed")
ALL_COLUMNS = COLUMNS + ("archive",)
COLUMN_OPTIONS = [(c.upper() if c != "review" else "REVIEW/QA", c) for c in ALL_COLUMNS]


class Confirm(BackdropModal[bool]):
    BACKDROP_RESULT = False

    def __init__(self, title: str):
        super().__init__()
        self.title = title

    def compose(self) -> ComposeResult:
        with Vertical(classes="sm"):
            with Horizontal(classes="modal-header"):
                yield Static("CONFIRM", classes="modal-name")
                yield Button("×", id="no")
            yield Static(self.title, classes="modal-text")
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel", id="cancel")
                yield Button("Confirm", id="yes", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class LogView(BackdropModal[None]):
    def __init__(self, ticket: str, slot: str):
        super().__init__()
        self.ticket = ticket
        self.slot = slot

    def compose(self) -> ComposeResult:
        with Vertical(classes="full"):
            with Horizontal(classes="modal-header"):
                yield Static(
                    f"SESSION OUTPUT · {self.ticket}/{self.slot}", classes="modal-name"
                )
                yield Button("× Close", id="close")
            with VerticalScroll(id="log-scroll"):
                yield Static("Loading...", id="log-text")
            yield Button("Refresh", id="refresh")

    async def on_mount(self) -> None:
        await self.refresh_log()

    async def refresh_log(self) -> None:
        try:
            response = await self.app.rpc(
                {"op": "log", "ticket": self.ticket, "slot": self.slot}
            )
            self.query_one("#log-text", Static).update(
                response.get("log") or "No log yet."
            )
        except RuntimeError as exc:
            self.app.notify(str(exc), severity="error")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close":
            self.dismiss(None)
        elif event.button.id == "refresh":
            await self.refresh_log()


class TicketView(BackdropModal[None]):
    """Read-only ticket content; the user edits the Markdown file externally."""

    def __init__(self, ticket: dict[str, Any]):
        super().__init__()
        self.ticket = ticket

    def compose(self) -> ComposeResult:
        state = self.ticket.get("state") or {}
        with Vertical(classes="full"):
            with Horizontal(classes="modal-header"):
                yield Static(
                    "TICKET " + self.ticket["id"] + " · " + self.ticket["title"],
                    classes="modal-name",
                    id="ticket-heading",
                )
                yield Button("× Close", id="close")
            with VerticalScroll(classes="content"):
                yield Static(
                    "FILE  "
                    + self.ticket["filename"]
                    + "   •   "
                    + self.ticket["column"].upper(),
                    classes="hint",
                    id="ticket-location",
                )
                yield Markdown(self.ticket["body"], id="ticket-markdown")
                yield Static("HARNESS SLOTS", classes="section-title")
                with Horizontal(classes="slot-row"):
                    yield Static("Developer", classes="slot-name")
                    yield Button("Configure", id="dev-config")
                    yield Button("Logs", id="dev-log")
                    yield Static(
                        (state.get("runtime") or {})
                        .get("developer", {})
                        .get("status", "idle"),
                        classes="runtime",
                        id="developer-runtime",
                    )
                for slot in state.get("reviewers") or []:
                    sid = slot.get("id", "reviewer")
                    with Horizontal(classes="slot-row"):
                        yield Static(sid, classes="slot-name")
                        yield Button("Configure", id="conf-" + sid)
                        yield Button("Logs", id="log-" + sid)
                        yield Button("Remove", id="remove-" + sid)
                yield Button("+ Reviewer", id="add-reviewer")
            yield Static(
                "Ticket Markdown is read-only here. Edit its .md file in your editor.",
                classes="hint",
            )

    def on_mount(self) -> None:
        self.set_interval(1.5, self.refresh_ticket)

    async def refresh_ticket(self) -> None:
        """Mirror external file changes without editing or recreating the dialog."""
        latest = self.app.find_ticket(self.ticket["id"])
        if latest is None:
            self.query_one("#ticket-location", Static).update(
                "Ticket is no longer present on this board"
            )
            return
        if latest["body"] != self.ticket["body"]:
            await self.query_one("#ticket-markdown", Markdown).update(latest["body"])
        if (
            latest["filename"] != self.ticket["filename"]
            or latest["column"] != self.ticket["column"]
        ):
            self.query_one("#ticket-location", Static).update(
                "FILE  " + latest["filename"] + "   •   " + latest["column"].upper()
            )
        if latest["title"] != self.ticket["title"]:
            self.query_one("#ticket-heading", Static).update(
                "TICKET " + latest["id"] + " · " + latest["title"]
            )
        dev = (latest.get("state") or {}).get("runtime") or {}
        self.query_one("#developer-runtime", Static).update(
            dev.get("developer", {}).get("status", "idle")
        )
        self.ticket = latest

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        name = event.button.id or ""
        if name == "close":
            self.dismiss(None)
            return
        if name == "dev-config":
            self.app.push_screen(Assignment(self.ticket, "developer"))
        elif name == "dev-log":
            self.app.push_screen(LogView(self.ticket["id"], "developer"))
        elif name.startswith("conf-"):
            self.app.push_screen(Assignment(self.ticket, name[5:]))
        elif name.startswith("log-"):
            self.app.push_screen(LogView(self.ticket["id"], name[4:]))
        elif name.startswith("remove-"):
            self.confirm_remove(name[7:])
        elif name == "add-reviewer":
            reviewers = (self.ticket.get("state") or {}).get("reviewers") or []
            occupied = {r.get("id") for r in reviewers}
            number = 1
            while "reviewer-" + str(number) in occupied:
                number += 1
            self.app.push_screen(Assignment(self.ticket, "reviewer-" + str(number)))

    @work
    async def confirm_remove(self, slot: str) -> None:
        approved = await self.app.push_screen_wait(
            Confirm("Remove reviewer slot " + slot + "?")
        )
        if approved:
            if await self.app.execute(
                {"op": "remove_reviewer", "ticket": self.ticket["id"], "slot": slot}
            ):
                self.dismiss(None)


class Assignment(BackdropModal[None]):
    def __init__(self, ticket: dict[str, Any], slot: str):
        super().__init__()
        self.ticket = ticket
        self.slot = slot

    def compose(self) -> ComposeResult:
        state = self.ticket.get("state") or {}
        value = (
            state.get("developer", {})
            if self.slot == "developer"
            else next(
                (r for r in state.get("reviewers", []) if r.get("id") == self.slot), {}
            )
        )
        with Vertical(classes="sm"):
            with Horizontal(classes="modal-header"):
                yield Static("SLOT CONFIGURATION · " + self.slot, classes="modal-name")
                yield Button("×", id="close")
            yield Static("Harness", classes="field")
            yield Select(
                [("Project default", "")] + HARNESS,
                value=value.get("harness") or "",
                id="harness",
            )
            yield Static("Custom Prompt", classes="field")
            yield TextArea(str(value.get("cp") or ""), id="cp")
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel", id="cancel")
                yield Button("Save", id="save", variant="primary")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        name = event.button.id
        if name in ("close", "cancel"):
            self.dismiss(None)
        elif name == "save":
            harness = self.query_one("#harness", Select).value
            cp = self.query_one("#cp", TextArea).text
            ok = await self.app.execute(
                {
                    "op": "set_assignment",
                    "ticket": self.ticket["id"],
                    "slot": self.slot,
                    "values": {"harness": harness, "cp": cp},
                }
            )
            if ok:
                self.dismiss(None)


class TicketActions(BackdropModal[None]):
    def __init__(self, ticket: dict[str, Any]):
        super().__init__()
        self.ticket = ticket

    def compose(self) -> ComposeResult:
        with Vertical(classes="sm"):
            with Horizontal(classes="modal-header"):
                yield Static(
                    "TICKET ACTIONS · " + self.ticket["id"], classes="modal-name"
                )
                yield Button("×", id="close")
            yield Static("Move to column", classes="field")
            yield Select(COLUMN_OPTIONS, value=self.ticket["column"], id="column")
            with Horizontal(classes="modal-actions"):
                yield Button("Move", id="move", variant="primary")
                yield Button("↑ Order", id="up")
                yield Button("↓ Order", id="down")
                yield Button("Archive", id="archive", variant="error")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        name = event.button.id
        if name == "close":
            self.dismiss(None)
            return
        ticket_id = self.ticket["id"]
        if name == "move":
            cmd = {
                "op": "move",
                "ticket": ticket_id,
                "column": self.query_one("#column", Select).value,
            }
        elif name == "archive":
            cmd = {"op": "archive", "ticket": ticket_id}
        else:
            cmd = {
                "op": "reorder",
                "ticket": ticket_id,
                "direction": -1 if name == "up" else 1,
            }
        if await self.app.execute(cmd):
            self.dismiss(None)


class NewTicket(BackdropModal[None]):
    def compose(self) -> ComposeResult:
        with Vertical(classes="sm"):
            with Horizontal(classes="modal-header"):
                yield Static("NEW TICKET", classes="modal-name")
                yield Button("×", id="close")
            yield Static("Title", classes="field")
            yield Input(id="title", placeholder="Implement authentication")
            yield Static("Initial column", classes="field")
            yield Select(
                [("BACKLOG", "backlog"), ("TODO", "todo")], value="backlog", id="column"
            )
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel", id="cancel")
                yield Button("Create", id="save", variant="primary")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id in ("close", "cancel"):
            self.dismiss(None)
        elif event.button.id == "save":
            title = self.query_one("#title", Input).value.strip()
            if not title:
                self.app.notify("Title is required", severity="warning")
                return
            if await self.app.execute(
                {
                    "op": "create",
                    "title": title,
                    "column": self.query_one("#column", Select).value,
                }
            ):
                self.dismiss(None)


class ArchiveView(VerticalScroll):
    def __init__(self, snapshot: dict[str, Any]):
        super().__init__(id="archive-page")
        self.snapshot = snapshot

    def compose(self) -> ComposeResult:
        yield Static("ARCHIVE / OUT OF SCOPE", classes="page-heading")
        for t in self.snapshot.get("tickets", []):
            if t["column"] != "archive":
                continue
            with Horizontal(classes="archive-row"):
                yield Static(t["title"] + " · " + t["id"], classes="archive-title")
                yield Button("View", id="view-" + t["id"])
                yield Button("Backlog", id="restore-" + t["id"])
                yield Button("Delete", id="delete-" + t["id"], variant="error")


class KanbeatApp(App[None]):
    TITLE = "Kanbeat"
    CSS_PATH = "styles.tcss"
    BINDINGS = [
        ("1", "tab('board')", "Board"),
        ("2", "tab('sessions')", "Sessions"),
        ("3", "tab('archive')", "Archive"),
        ("4", "tab('settings')", "Settings"),
        ("ctrl+r", "refresh", "Refresh"),
        ("ctrl+q", "quit", "Quit TUI"),
    ]

    def __init__(self, project: Path):
        super().__init__()
        self.project = project
        self.preferences = ViewPreferences(project)
        self.tab = "board"
        self.snapshot: dict[str, Any] = {}
        self._signature = ""
        self._updating = False
        self.select_mode = False
        self.selection: set[str] = set()
        self._seen_notice_id = 0
        self._initial_snapshot = True

    def compose(self) -> ComposeResult:
        with Vertical(id="root"):
            with Horizontal(id="nav"):
                yield Static("◉ KANBEAT", id="logo")
                for tab in ("board", "sessions", "archive", "settings"):
                    yield Button(tab.title(), id="nav-" + tab, classes="nav-button")
                yield Button("▶ PLAY", id="execution", variant="success")
            yield Vertical(id="content")
            yield Static("Local supervisor · " + str(self.project), id="bottom")

    async def on_mount(self) -> None:
        splash = StartupScreen()
        self.push_screen(splash)
        started = asyncio.get_running_loop().time()
        try:
            await self.refresh_snapshot()
            minimum = 0 if self.animation_level == "none" else 0.45
            await asyncio.sleep(
                max(0, minimum - (asyncio.get_running_loop().time() - started))
            )
        finally:
            if self.screen is splash:
                self.pop_screen()
            self.set_interval(1.5, self.refresh_snapshot)

    async def rpc(self, message: dict[str, Any]) -> dict[str, Any]:
        return await request(self.project, message)

    async def execute(self, message: dict[str, Any]) -> bool:
        try:
            await self.rpc(message)
            await self.refresh_snapshot(force=True)
            return True
        except (RuntimeError, ValueError, OSError) as exc:
            self.notify(str(exc), severity="error", timeout=7)
            return False

    async def refresh_snapshot(self, force: bool = False) -> None:
        if self._updating:
            return
        self._updating = True
        try:
            snap = await self.rpc({"op": "snapshot"})
            signature = json.dumps(snap, sort_keys=True, ensure_ascii=False)
            self.snapshot = snap
            execution_button = self.query_one("#execution", Button)
            execution_button.label = (
                "■ STOP" if snap.get("execution") == "running" else "▶ PLAY"
            )
            execution_button.variant = (
                "error" if snap.get("execution") == "running" else "success"
            )
            notices = snap.get("notices") or []
            if self._initial_snapshot:
                self._seen_notice_id = max(
                    (int(n.get("id", 0)) for n in notices), default=0
                )
                self._initial_snapshot = False
            for notice in notices:
                sequence = int(notice.get("id", 0))
                if sequence > self._seen_notice_id:
                    self.notify(
                        str(notice.get("message", "")),
                        severity=notice.get("level", "information"),
                        timeout=8,
                    )
                    self._seen_notice_id = max(self._seen_notice_id, sequence)
            if signature != self._signature or force:
                self._signature = signature
                await self.render_page()
        except (RuntimeError, OSError) as exc:
            self.notify(str(exc), severity="error", timeout=4)
        finally:
            self._updating = False

    async def render_page(self) -> None:
        panel = self.query_one("#content", Vertical)
        current = panel.children[0] if panel.children else None
        previous_focus = self.focused
        focused_id = previous_focus.id if previous_focus is not None else None
        with self.batch_update():
            if self.tab == "board" and isinstance(current, BoardView):
                await current.update_snapshot(
                    self.snapshot, self.select_mode, self.selection
                )
            elif self.tab == "sessions" and isinstance(current, SessionsView):
                await current.update_snapshot(self.snapshot)
            elif self.tab == "settings" and isinstance(current, Settings):
                current.update_snapshot(self.snapshot)
            elif self.tab == "archive" and isinstance(current, ArchiveView):
                old = [
                    t
                    for t in current.snapshot.get("tickets", [])
                    if t["column"] == "archive"
                ]
                new = [
                    t
                    for t in self.snapshot.get("tickets", [])
                    if t["column"] == "archive"
                ]
                if old != new:
                    await panel.remove_children()
                    await panel.mount(ArchiveView(self.snapshot))
            else:
                await panel.remove_children()
                if self.tab == "board":
                    await panel.mount(
                        BoardView(self.snapshot, self.select_mode, self.selection)
                    )
                elif self.tab == "sessions":
                    await panel.mount(SessionsView(self.snapshot))
                elif self.tab == "archive":
                    await panel.mount(ArchiveView(self.snapshot))
                else:
                    await panel.mount(Settings(self.snapshot))
        if focused_id:
            target = next(iter(panel.query("#" + focused_id)), None)
            if target is not None and target is not previous_focus:
                target.focus(scroll_visible=False)
        for tab in ("board", "sessions", "archive", "settings"):
            self.query_one("#nav-" + tab, Button).set_class(tab == self.tab, "selected")

    def action_tab(self, tab: str) -> None:
        self.tab = tab
        self.call_later(self.render_page)

    def action_refresh(self) -> None:
        self.call_later(self.refresh_snapshot, True)

    def find_ticket(self, ticket_id: str) -> dict[str, Any] | None:
        return next(
            (t for t in self.snapshot.get("tickets", []) if t["id"] == ticket_id), None
        )

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        name = event.button.id or ""
        if name.startswith("nav-"):
            self.tab = name[4:]
            await self.render_page()
        elif name == "execution":
            if self.snapshot.get("execution") == "running":
                await self.execute({"op": "stop"})
            else:
                await self.execute({"op": "play"})
        elif name == "select-mode":
            self.select_mode = not self.select_mode
            self.selection.clear()
            await self.render_page()
        elif name.startswith("pick-"):
            ticket_id = name[5:]
            if ticket_id in self.selection:
                self.selection.remove(ticket_id)
            else:
                self.selection.add(ticket_id)
            await self.render_page()
        elif name == "bulk-archive":
            self.confirm_bulk_archive()
        elif name == "new-ticket":
            self.push_screen(NewTicket())
        elif name.startswith("view-"):
            ticket = self.find_ticket(name[5:])
            if ticket:
                self.push_screen(TicketView(ticket))
        elif name.startswith("act-"):
            ticket = self.find_ticket(name[4:])
            if ticket:
                self.push_screen(TicketActions(ticket))
        elif name.startswith("restore-"):
            await self.execute(
                {"op": "restore", "ticket": name[8:], "column": "backlog"}
            )
        elif name.startswith("delete-"):
            self.confirm_delete(name[7:])
        elif name.startswith("term-"):
            _, tid, slot = name.split("-", 2)
            try:
                result = await self.rpc(
                    {"op": "terminal_open", "ticket": tid, "slot": slot}
                )
                self.push_screen(TerminalView(result["terminal"]))
            except (RuntimeError, ValueError, OSError) as exc:
                self.notify(str(exc), severity="error")
        elif name.startswith("log-"):
            _, tid, slot = name.split("-", 2)
            await self.push_screen(LogView(tid, slot))
        elif name == "settings-save":
            config = {"default_harness": self.query_one("#cfg-harness", Select).value}
            for field in ("global_cp", "developer_prompt", "reviewer_prompt"):
                config[field] = self.query_one("#cfg-" + field, TextArea).text
            config["transition_hook"] = {
                "harness": self.query_one("#cfg-hook-harness", Select).value,
                "prompt": self.query_one("#cfg-hook-prompt", TextArea).text,
            }
            await self.execute({"op": "save_config", "values": config})

    @work
    async def confirm_bulk_archive(self) -> None:
        selected = sorted(self.selection)
        if not selected:
            return
        approved = await self.push_screen_wait(Confirm("Archive selected tickets?"))
        if approved:
            for ticket_id in selected:
                await self.execute({"op": "archive", "ticket": ticket_id})
            self.selection.clear()
            self.select_mode = False
            await self.render_page()

    @work
    async def confirm_delete(self, ticket_id: str) -> None:
        approved = await self.push_screen_wait(
            Confirm("Permanently delete archived ticket " + ticket_id + "?")
        )
        if approved:
            await self.execute({"op": "delete", "ticket": ticket_id})

"""Kanbeat TUI. A mouse-clickable view/controller for the supervisor, never its owner."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from .modal import BackdropModal
from textual.geometry import Region
from textual.widgets import Button, Input, Markdown, Select, Static, TextArea
from textual.css.query import NoMatches

from ..supervisor import request
from .terminal import TerminalPane, TerminalView
from .sessions import PopupMenu, SessionsView
from .session_rows import live_session, worker_is_active, worker_status_label
from .options import HARNESS
from .board import BoardView, dependency_text, dependency_widgets
from .preferences import ViewPreferences
from .settings import Settings
from .startup import StartupScreen

COLUMNS = ("backlog", "todo", "progress", "review", "done", "failed")
FREE_STEPS = {("backlog", "todo"), ("todo", "backlog")}


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
                yield Button("×", id="close")
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

    @staticmethod
    def runtime_label(state: dict[str, Any], slot: str) -> str:
        entry = (state.get("runtime") or {}).get(slot)
        return worker_status_label(entry) if entry else "not started"

    @staticmethod
    def active_worker(state: dict[str, Any], slot: str) -> bool:
        entry = (state.get("runtime") or {}).get(slot)
        return bool(entry and worker_is_active(entry))

    def status_classes(self, state: dict[str, Any], slot: str) -> str:
        active = self.app.snapshot.get("execution") == "running" and self.active_worker(
            state, slot
        )
        return "runtime agent-working" if active else "runtime"

    def compose(self) -> ComposeResult:
        state = self.ticket.get("state") or {}
        with Vertical(classes="full"):
            with Horizontal(classes="modal-header"):
                yield Static(
                    "TICKET " + self.ticket["id"] + " · " + self.ticket["title"],
                    classes="modal-name",
                    id="ticket-heading",
                )
                yield Button("×", id="close")
            with VerticalScroll(classes="content"):
                yield Static(
                    "FILE  "
                    + self.ticket["filename"]
                    + "   •   "
                    + self.ticket["column"].upper(),
                    classes="hint",
                    id="ticket-location",
                )
                with Vertical(id="ticket-links") as links:
                    yield from dependency_widgets(self.ticket)
                links.display = bool(dependency_text(self.ticket))
                yield Markdown(self.ticket["body"], id="ticket-markdown")
                yield Static("HARNESS SLOTS", classes="section-title")
                with Horizontal(classes="slot-row"):
                    yield Static("Developer", classes="slot-name")
                    yield Button("Configure", id="dev-config")
                    yield Button(
                        "Terminal",
                        id="dev-terminal",
                        disabled=(
                            self.app.snapshot.get("execution") == "running"
                            and live_session(self.ticket, "developer") is None
                        ),
                    )
                    yield Button("Logs", id="dev-log")
                    yield Static(
                        self.runtime_label(state, "developer"),
                        classes=self.status_classes(state, "developer"),
                        id="developer-runtime",
                    )
                for slot in state.get("reviewers") or []:
                    sid = slot.get("id", "reviewer")
                    with Horizontal(classes="slot-row"):
                        yield Static(sid, classes="slot-name")
                        yield Button("Configure", id="conf-" + sid)
                        yield Button(
                            "Terminal",
                            id="terminal-" + sid,
                            disabled=(
                                self.app.snapshot.get("execution") == "running"
                                and live_session(self.ticket, sid) is None
                            ),
                        )
                        yield Button("Logs", id="log-" + sid)
                        yield Button("Remove", id="remove-" + sid)
                        yield Static(
                            self.runtime_label(state, sid),
                            classes=self.status_classes(state, sid),
                            id="runtime-" + sid,
                        )
                yield Button("+ Reviewer", id="add-reviewer")
                yield Static(
                    "OpenCode, Codex and Claude sessions reattach live. Pi requires Board Stop.",
                    classes="hint",
                )
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
        if dependency_text(latest) != dependency_text(self.ticket):
            links = self.query_one("#ticket-links", Vertical)
            await links.remove_children()
            await links.mount_all(dependency_widgets(latest))
            links.display = bool(dependency_text(latest))
        if latest["title"] != self.ticket["title"]:
            self.query_one("#ticket-heading", Static).update(
                "TICKET " + latest["id"] + " · " + latest["title"]
            )
        state = latest.get("state") or {}
        developer = self.query_one("#developer-runtime", Static)
        developer.update(self.runtime_label(state, "developer"))
        active = self.active_worker(state, "developer") and (
            self.app.snapshot.get("execution") == "running"
        )
        developer.set_class(active, "agent-working")
        if not active:
            developer.remove_class("agent-pulse")
        running = self.app.snapshot.get("execution") == "running"
        self.query_one("#dev-terminal", Button).disabled = running and (
            live_session(latest, "developer") is None
        )
        for slot in state.get("reviewers") or []:
            label = self.query("#runtime-" + slot["id"])
            if label:
                widget = label.first(Static)
                widget.update(self.runtime_label(state, slot["id"]))
                active = self.active_worker(state, slot["id"]) and (
                    self.app.snapshot.get("execution") == "running"
                )
                widget.set_class(active, "agent-working")
                if not active:
                    widget.remove_class("agent-pulse")
            button = self.query("#terminal-" + slot["id"])
            if button:
                button.first(Button).disabled = running and (
                    live_session(latest, slot["id"]) is None
                )
        self.ticket = latest

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        name = event.button.id or ""
        if name == "close":
            self.dismiss(None)
            return
        if event.button.has_class("dependency"):
            target = self.app.find_ticket(event.button.name or "")
            if target is not None:
                app = self.app
                self.dismiss(None)
                app.push_screen(TicketView(target))
            return
        if name == "dev-config":
            self.app.push_screen(Assignment(self.ticket, "developer"))
        elif name == "dev-terminal":
            await self.app.open_task_terminal(self.ticket["id"], "developer")
        elif name == "dev-log":
            self.app.push_screen(LogView(self.ticket["id"], "developer"))
        elif name.startswith("conf-"):
            self.app.push_screen(Assignment(self.ticket, name[5:]))
        elif name.startswith("terminal-"):
            await self.app.open_task_terminal(
                self.ticket["id"], name[len("terminal-") :]
            )
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
        with VerticalScroll(classes="sm"):
            with Horizontal(classes="modal-header"):
                yield Static("SLOT CONFIGURATION · " + self.slot, classes="modal-name")
                yield Button("×", id="close")
            yield Static("Harness", classes="field")
            with Horizontal(classes="field-row"):
                yield Select(
                    [("Project default", "")] + HARNESS,
                    value=value.get("harness") or "",
                    id="harness",
                )
                yield Button("Open", id="open-harness")
            yield Static("Custom Prompt", classes="field")
            yield TextArea(str(value.get("cp") or ""), id="cp")
            yield Static(
                "Terminal uses saved slot settings and reattaches live OpenCode, Codex or Claude sessions; "
                "other harnesses require Board Stop. Open starts a separate harness "
                "configuration session and does not save these edits.",
                classes="hint",
            )
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel", id="cancel")
                yield Button(
                    "Terminal",
                    id="open-terminal",
                    disabled=(
                        self.app.snapshot.get("execution") == "running"
                        and live_session(self.ticket, self.slot) is None
                    ),
                )
                yield Button("Save", id="save", variant="primary")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        name = event.button.id
        if name in ("close", "cancel"):
            self.dismiss(None)
        elif name == "open-terminal":
            state = self.ticket.get("state") or {}
            saved = (
                state.get("developer", {})
                if self.slot == "developer"
                else next(
                    (
                        row
                        for row in state.get("reviewers", [])
                        if row.get("id") == self.slot
                    ),
                    {},
                )
            )
            if self.query_one("#harness", Select).value != (
                saved.get("harness") or ""
            ) or self.query_one("#cp", TextArea).text != str(saved.get("cp") or ""):
                self.app.notify(
                    "Save slot changes before opening its terminal", severity="warning"
                )
                return
            await self.app.open_task_terminal(self.ticket["id"], self.slot)
        elif name == "open-harness":
            await self.app.open_harness_config(self.query_one("#harness", Select).value)
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


class ColumnMenu(PopupMenu):
    def __init__(self, ticket: dict[str, Any], anchor: Region | None = None):
        super().__init__(anchor)
        self.ticket = ticket

    def compose(self) -> ComposeResult:
        with Vertical(classes="menu"):
            yield Static("#" + self.ticket["id"], classes="section-title")
            for column in COLUMNS:
                yield Button(
                    "REVIEW/QA" if column == "review" else column.upper(),
                    id="jump-" + column,
                    disabled=column == self.ticket["column"],
                )
            yield Button("ARCHIVE", id="jump-archive", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        target = (event.button.id or "")[len("jump-") :]
        ticket_id = self.ticket["id"]
        app = self.app
        self.dismiss(None)
        if target == "archive":
            app.confirm_archive(ticket_id)
        else:
            app.move_by_hand(ticket_id, target)


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


TABS = ("sessions", "board", "archive", "settings")


class KanbeatApp(App[None]):
    TITLE = "Kanbeat"
    CSS_PATH = "styles.tcss"
    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (136, "-wide")]
    BINDINGS = [
        ("1", "tab('sessions')", "Sessions"),
        ("2", "tab('board')", "Board"),
        ("3", "tab('archive')", "Archive"),
        ("4", "tab('settings')", "Settings"),
        ("ctrl+r", "refresh", "Refresh"),
        ("ctrl+q", "quit", "Quit TUI"),
    ]

    def __init__(self, project: Path):
        super().__init__()
        self.project = project
        self.preferences = ViewPreferences(project)
        self.tab = TABS[0]
        self.snapshot: dict[str, Any] = {}
        self._signature = ""
        self._updating = False
        self.select_mode = False
        self.selection: set[str] = set()
        self._seen_notice_id = 0
        self._initial_snapshot = True
        self._pulse_bright = False
        self.embedded_terminal: str | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="root"):
            with Horizontal(id="nav"):
                with Horizontal(id="brand"):
                    yield Static("◉ KANBEAT", id="logo")
                    yield Static("", id="terminal-heading")
                with Horizontal(id="terminal-controls"):
                    yield Button("Live", id="terminal-live", disabled=True)
                    yield Button("Stop", id="terminal-stop", variant="error")
                    yield Button("Detach", id="terminal-detach")
                yield Static("", id="nav-spacer")
                for tab in TABS:
                    yield Button(tab.title(), id="nav-" + tab, classes="nav-button")
                yield Button("▶ PLAY", id="execution", variant="success")
                yield Button("Quit", id="quit")
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
            self.set_interval(0.8, self.pulse_working)

    def pulse_working(self) -> None:
        """Pulse only confirmed running workers, without rebuilding the board."""
        self._pulse_bright = not self._pulse_bright
        bright = self.animation_level != "none" and self._pulse_bright
        if not self.screen_stack:
            return
        for label in self.screen.query(".agent-working"):
            label.set_class(bright, "agent-pulse")

    async def rpc(self, message: dict[str, Any]) -> dict[str, Any]:
        return await request(self.project, message)

    async def put_on_clipboard(self, text: str) -> None:
        self.copy_to_clipboard(text)
        tool = next((name for name in ("wl-copy", "xclip") if shutil.which(name)), None)
        if tool is None:
            return
        command = [tool] + (["-selection", "clipboard"] if tool == "xclip" else [])
        try:
            await asyncio.to_thread(
                subprocess.run,
                command,
                input=text.encode(),
                timeout=3,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    async def copy_selection(self, text: str) -> None:
        await self.put_on_clipboard(text)
        self.notify(f"Copied selection, {len(text)} characters")

    async def resume_session(self, terminal_id: str) -> None:
        entry = self.snapshot.get("terminals", {}).get(terminal_id)
        if entry is None or entry.get("status") == "stopping":
            return
        if not entry.get("attached") or entry.get("status") not in (
            "starting",
            "running",
        ):
            try:
                result = await self.rpc(
                    {"op": "session_restart", "terminal": terminal_id}
                )
            except (RuntimeError, ValueError, OSError) as exc:
                self.notify(str(exc), severity="error", timeout=7)
                return
            terminal_id = result["terminal"]
            await self.refresh_snapshot(force=True)
        self.open_session_terminal(terminal_id)

    def forget_embedded(self, terminal_id: str) -> None:
        if self.embedded_terminal != terminal_id:
            return
        pane = next(iter(self.screen_stack[0].query(TerminalPane)), None)
        if pane is not None:
            pane.leave()

    def open_session_terminal(self, terminal_id: str) -> None:
        pages = (
            self.screen_stack[0].query(SessionsView) if self.tab == "sessions" else []
        )
        page = next(iter(pages), None)
        if page is not None and page.can_embed:
            page.show_terminal(terminal_id)
        else:
            self.push_screen(TerminalView(terminal_id))

    async def open_task_terminal(self, ticket_id: str, slot: str) -> None:
        ticket = next(
            (row for row in self.snapshot.get("tickets", []) if row["id"] == ticket_id),
            None,
        )
        op = (
            "terminal_attach"
            if ticket is not None and live_session(ticket, slot)
            else "terminal_open"
        )
        try:
            result = await self.rpc({"op": op, "ticket": ticket_id, "slot": slot})
            self.open_session_terminal(result["terminal"])
        except (RuntimeError, ValueError, OSError) as exc:
            self.notify(str(exc), severity="error", timeout=7)

    async def open_harness_config(self, harness: Any) -> None:
        if not isinstance(harness, str):
            harness = ""
        try:
            result = await self.rpc({"op": "session_config_open", "harness": harness})
            await self.refresh_snapshot(force=True)
            self.push_screen(TerminalView(result["terminal"]))
        except (RuntimeError, ValueError, OSError) as exc:
            self.notify(str(exc), severity="error", timeout=7)

    async def execute(self, message: dict[str, Any]) -> bool:
        try:
            await self.rpc(message)
            await self.refresh_snapshot(force=True)
            return True
        except (RuntimeError, ValueError, OSError) as exc:
            self.notify(str(exc), severity="error", timeout=7)
            return False

    async def refresh_snapshot(self, force: bool = False) -> None:
        if self._updating or not self.is_mounted:
            return
        self._updating = True
        try:
            snap = await self.rpc({"op": "snapshot"})
            signature = json.dumps(snap, sort_keys=True, ensure_ascii=False)
            self.snapshot = snap
            try:
                execution_button = self.query_one("#execution", Button)
            except NoMatches:
                return
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
        for tab in TABS:
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
        elif name.startswith("terminal-"):
            pane = next(iter(self.screen_stack[0].query(TerminalPane)), None)
            if pane is not None:
                await pane.press_control(name)
        elif name == "quit":
            self.exit()
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
        elif name.startswith("menu-"):
            ticket = self.find_ticket(name[5:])
            if ticket:
                self.push_screen(ColumnMenu(ticket, event.button.region))
        elif name.startswith(("left-", "right-")):
            direction, ticket_id = name.split("-", 1)
            self.step_ticket(ticket_id, -1 if direction == "left" else 1)
        elif name.startswith("restore-"):
            await self.execute(
                {"op": "restore", "ticket": name[8:], "column": "backlog"}
            )
        elif name.startswith("delete-"):
            self.confirm_delete(name[7:])
        elif name.startswith("term-"):
            _, tid, slot = name.split("-", 2)
            await self.open_task_terminal(tid, slot)
        elif name.startswith("log-"):
            _, tid, slot = name.split("-", 2)
            await self.push_screen(LogView(tid, slot))
        elif name == "cfg-open-harness":
            await self.open_harness_config(self.query_one("#cfg-harness", Select).value)
        elif name == "cfg-open-hook-harness":
            await self.open_harness_config(
                self.query_one("#cfg-hook-harness", Select).value
            )
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
    async def confirm_archive(self, ticket_id: str) -> None:
        if await self.push_screen_wait(Confirm("Archive ticket #" + ticket_id + "?")):
            await self.execute({"op": "archive", "ticket": ticket_id})

    def step_ticket(self, ticket_id: str, direction: int) -> None:
        ticket = self.find_ticket(ticket_id)
        if ticket is None or ticket["column"] not in COLUMNS:
            return
        target = COLUMNS.index(ticket["column"]) + direction
        if 0 <= target < len(COLUMNS):
            self.move_by_hand(ticket_id, COLUMNS[target])

    @work
    async def move_by_hand(self, ticket_id: str, column: str) -> None:
        ticket = self.find_ticket(ticket_id)
        if ticket is None or ticket["column"] == column:
            return
        step = (ticket["column"], column)
        blockers = [
            "#" + row["id"]
            for row in ticket.get("blocked_by", [])
            if not row.get("satisfied")
        ]
        blocked = column == "progress" and bool(blockers)
        if step not in FREE_STEPS or blocked:
            question = (
                f"Move #{ticket_id} from {step[0].upper()} to {step[1].upper()} "
                "by hand? Kanbeat normally does this step itself."
            )
            if blocked:
                question += " It is still blocked by " + ", ".join(blockers) + "."
            if not await self.push_screen_wait(Confirm(question)):
                return
        await self.execute(
            {"op": "move", "ticket": ticket_id, "column": column, "force": blocked}
        )

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

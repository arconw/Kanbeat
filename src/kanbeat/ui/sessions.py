from __future__ import annotations

from typing import Any
from rich.text import Text
from rich.markup import escape

from textual import events, on, work
from textual.app import ComposeResult
from textual.geometry import Region
from textual.containers import Horizontal, Vertical, VerticalScroll
from .modal import BackdropModal
from textual.widgets import Button, Collapsible, Input, Select, Static, TextArea

from .options import HARNESS
from .terminal import TerminalPane
from .session_rows import SessionRow, SessionSection, TaskSessions

EMBED_MIN_WIDTH = 114


def group_title(group, count):
    return f"[{count}]: {group['name']}"


def group_options(snapshot):
    return [("Ungrouped", "")] + [
        (Text(row["name"]), key)
        for key, row in snapshot.get("custom", {}).get("groups", {}).items()
    ]


class GroupForm(BackdropModal[None]):
    def __init__(self, group_id: str = ""):
        super().__init__()
        self.group_id = group_id

    def compose(self) -> ComposeResult:
        groups = self.app.snapshot.get("custom", {}).get("groups", {})
        with Vertical(classes="sm"):
            yield Static(
                "RENAME GROUP" if self.group_id else "NEW SESSION GROUP",
                classes="section-title",
            )
            yield Input(
                groups.get(self.group_id, {}).get("name", ""),
                placeholder="Group name",
                id="group-name",
            )
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel", id="cancel")
                yield Button("Save", id="group-save", variant="primary")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "group-save":
            if await self.app.execute(
                {
                    "op": "group_rename" if self.group_id else "group_create",
                    "group": self.group_id,
                    "name": self.query_one("#group-name", Input).value,
                }
            ):
                self.dismiss(None)


class NewSession(BackdropModal[None]):
    starting = False

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="sm"):
            yield Static("NEW INDEPENDENT SESSION", classes="section-title")
            yield Input(placeholder="Session name", id="session-title")
            yield Static("Group", classes="field")
            yield Select(
                group_options(self.app.snapshot),
                value="",
                allow_blank=False,
                id="session-group",
            )
            yield Static("Native harness", classes="field")
            with Horizontal(classes="field-row"):
                yield Select(
                    HARNESS,
                    value=self.app.snapshot["config"]["default_harness"],
                    allow_blank=False,
                    id="session-harness",
                )
                yield Button("Open", id="open-harness")
            yield Static(
                "Native session ID (optional; empty starts fresh)", classes="field"
            )
            yield Input(id="session-native-id")
            yield Static("Initial prompt (optional)", classes="field")
            yield TextArea(id="session-prompt")
            yield Static(
                "Independent sessions keep running on Board Stop and when you detach.",
                classes="hint",
            )
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel", id="cancel")
                yield Button("Start", id="session-start", variant="primary")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "open-harness":
            await self.app.open_harness_config(
                self.query_one("#session-harness", Select).value
            )
        elif event.button.id == "session-start":
            if self.starting:
                return
            self.starting = True
            event.button.disabled = True
            try:
                result = await self.app.rpc(
                    {
                        "op": "session_create",
                        "title": self.query_one("#session-title", Input).value,
                        "harness": self.query_one("#session-harness", Select).value,
                        "group": self.query_one("#session-group", Select).value,
                        "native_session": self.query_one(
                            "#session-native-id", Input
                        ).value,
                        "prompt": self.query_one("#session-prompt", TextArea).text,
                    }
                )
                app = self.app
                await app.refresh_snapshot(force=True)
                if self in app.screen_stack:
                    self.dismiss(None)
                app.open_session_terminal(result["terminal"])
            except (ValueError, RuntimeError, OSError) as exc:
                self.app.notify(str(exc), severity="error")


class PopupMenu(BackdropModal[None]):
    BINDINGS = [("escape", "close", "Close")]

    def __init__(self, anchor: Region | None = None):
        super().__init__()
        self.anchor = anchor

    def on_mount(self) -> None:
        if self.anchor is not None:
            self.call_after_refresh(self.place_near_anchor)

    def action_close(self) -> None:
        self.dismiss(None)

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if not self.query_one(".menu").region.contains_point(event.screen_offset):
            event.stop()
            self.dismiss(None)

    def place_near_anchor(self) -> None:
        menu = self.query_one(".menu")
        screen = self.app.size
        x = min(self.anchor.right + 1, max(0, screen.width - menu.outer_size.width))
        y = min(self.anchor.y, max(0, screen.height - menu.outer_size.height))
        menu.styles.offset = (x, y)


class GroupActions(PopupMenu):
    def __init__(self, group_id: str, anchor: Region | None = None):
        super().__init__(anchor)
        self.group_id = group_id

    def compose(self) -> ComposeResult:
        groups = self.app.snapshot.get("custom", {}).get("groups", {})
        with Vertical(classes="menu"):
            yield Static(
                groups.get(self.group_id, {}).get("name", "GROUP"),
                classes="section-title",
                markup=False,
            )
            yield Button("Rename", id="group-rename")
            yield Button("Remove", id="group-remove", variant="error")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        name = event.button.id
        if name == "group-rename":
            app = self.app
            self.dismiss(None)
            app.push_screen(GroupForm(self.group_id))
        elif name == "group-remove":
            self.confirm_removal()

    @work
    async def confirm_removal(self) -> None:
        from .app import Confirm

        if await self.app.push_screen_wait(Confirm("Remove this empty session group?")):
            if await self.app.execute(
                {"op": "group_delete", "group": self.group_id, "confirmed": True}
            ):
                self.dismiss(None)


class SessionEdit(BackdropModal[None]):
    def __init__(self, entry: dict[str, Any]):
        super().__init__()
        self.entry = entry

    def compose(self) -> ComposeResult:
        with Vertical(classes="sm"):
            yield Static("EDIT SESSION", classes="section-title")
            yield Input(self.entry["title"], id="session-title")
            yield Select(
                group_options(self.app.snapshot),
                value=self.entry.get("group", ""),
                allow_blank=False,
                id="session-group",
            )
            yield Static("Native session ID (used to resume)", classes="field")
            yield Input(
                self.entry.get("native_session") or "",
                placeholder="Empty: the next start creates a new session",
                id="session-native-id",
            )
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel", id="cancel")
                yield Button("Save", id="session-save", variant="primary")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "session-save":
            if await self.app.execute(
                {
                    "op": "session_update",
                    "terminal": self.entry["id"],
                    "title": self.query_one("#session-title", Input).value,
                    "group": self.query_one("#session-group", Select).value,
                    "native_session": self.query_one(
                        "#session-native-id", Input
                    ).value.strip(),
                }
            ):
                self.dismiss(None)


class SessionActions(PopupMenu):
    def __init__(self, entry: dict[str, Any], anchor: Region | None = None):
        super().__init__(anchor)
        self.entry = entry

    def compose(self) -> ComposeResult:
        status = self.entry["status"]
        running = status in ("starting", "running")
        with Vertical(classes="menu"):
            yield Static(self.entry["title"], classes="section-title", markup=False)
            yield Button("Edit", id="session-edit")
            if running and self.entry.get("attached"):
                yield Button("Stop", id="session-stop")
            if status != "stopping":
                yield Button("Delete", id="session-remove", variant="error")
                if self.entry.get("native_session") and self.entry["harness"] in (
                    "claude",
                    "codex",
                    "opencode",
                    "pi",
                ):
                    yield Button(
                        "Delete completely", id="session-remove-all", variant="error"
                    )

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        name = event.button.id
        if name == "session-edit":
            app = self.app
            self.dismiss(None)
            app.push_screen(SessionEdit(self.entry))
        elif name == "session-stop":
            if await self.app.execute(
                {"op": "terminal_stop", "terminal": self.entry["id"], "confirmed": True}
            ):
                self.dismiss(None)
        elif name in ("session-remove", "session-remove-all"):
            self.confirm_delete(name == "session-remove-all")

    @work
    async def confirm_delete(self, native: bool) -> None:
        from .app import Confirm

        harness = self.entry["harness"]
        question = (
            f"Delete this session completely, including its history in {harness}? "
            "This cannot be undone."
            if native
            else f"Remove this session from the workspace? Its history in {harness} is kept."
        )
        app = self.app
        if not await app.push_screen_wait(Confirm(question)):
            return
        terminal_id = self.entry["id"]
        if self.entry["status"] in ("starting", "running") and not await app.execute(
            {"op": "terminal_stop", "terminal": terminal_id, "confirmed": True}
        ):
            return
        if await app.execute(
            {
                "op": "session_delete",
                "terminal": terminal_id,
                "confirmed": True,
                "native": native,
            }
        ):
            app.forget_embedded(terminal_id)
            if self in app.screen_stack:
                self.dismiss(None)


class SessionList(VerticalScroll):
    def __init__(self, snapshot: dict[str, Any]):
        super().__init__(id="sessions-list")
        self.snapshot = snapshot

    def entries(self, group_id):
        return [
            entry
            for entry in self.snapshot.get("terminals", {}).values()
            if entry.get("owner") == "custom" and entry.get("group", "") == group_id
        ]

    @staticmethod
    def groups(snapshot):
        return {
            **snapshot.get("custom", {}).get("groups", {}),
            "": {"name": "Ungrouped"},
        }

    def group_section(self, group_id, group):
        key = "group-" + (group_id or "ungrouped")
        entries = self.entries(group_id)
        section = SessionSection(
            key,
            group_title(group, len(entries)),
            self.app.preferences.is_collapsed(key),
            "group-actions-" + group_id if group_id else "",
        )
        for entry in entries:
            section.compose_add_child(SessionRow(entry))
        return section

    def compose(self) -> ComposeResult:
        with SessionSection(
            "tasks", "Tasks", self.app.preferences.is_collapsed("tasks", True)
        ):
            for ticket in self.snapshot.get("tickets", []):
                yield TaskSessions(
                    ticket,
                    self.snapshot.get("execution") == "running",
                    self.snapshot.get("terminals", {}),
                )
        for group_id, group in self.groups(self.snapshot).items():
            yield self.group_section(group_id, group)

    @on(Collapsible.Toggled)
    def remember_collapse(self, event: Collapsible.Toggled) -> None:
        section = event.collapsible
        if isinstance(section, SessionSection):
            event.stop()
            try:
                self.app.preferences.remember(section.name, section.collapsed)
            except OSError as exc:
                self.app.notify(
                    "Could not save session layout: " + str(exc), severity="error"
                )

    async def update_snapshot(self, snapshot):
        self.snapshot = snapshot
        tasks = self.query_one("#section-tasks > Contents")
        wanted_tasks = {ticket["id"]: ticket for ticket in snapshot.get("tickets", [])}
        current_tasks = {row.ticket["id"]: row for row in tasks.query(TaskSessions)}
        for ticket_id, row in current_tasks.items():
            if ticket_id not in wanted_tasks:
                await row.remove()
        for ticket_id, ticket in wanted_tasks.items():
            if ticket_id in current_tasks:
                await current_tasks[ticket_id].update_ticket(
                    ticket,
                    snapshot.get("execution") == "running",
                    snapshot.get("terminals", {}),
                )
            else:
                await tasks.mount(
                    TaskSessions(
                        ticket,
                        snapshot.get("execution") == "running",
                        snapshot.get("terminals", {}),
                    )
                )
        order = {
            "task-sessions-" + key: index for index, key in enumerate(wanted_tasks)
        }
        if [row.id for row in tasks.query(TaskSessions)] != list(order):
            tasks.sort_children(key=lambda row: order.get(row.id, -1))
        groups = self.groups(snapshot)
        wanted_sections = {"section-group-" + (key or "ungrouped") for key in groups}
        for section in list(self.query(SessionSection)):
            if section.id != "section-tasks" and section.id not in wanted_sections:
                await section.remove()
        wanted_entries = snapshot.get("terminals", {})
        for row in list(self.query(SessionRow)):
            entry = wanted_entries.get(row.entry["id"])
            if entry is None or entry.get("group", "") != row.entry.get("group", ""):
                await row.remove()
        for group_id, group in groups.items():
            section_id = "section-group-" + (group_id or "ungrouped")
            sections = list(self.query("#" + section_id))
            if not sections:
                ungrouped = next(iter(self.query("#section-group-ungrouped")), None)
                await self.mount(
                    self.group_section(group_id, group),
                    before=ungrouped if group_id else None,
                )
                continue
            section = sections[0]
            entries = self.entries(group_id)
            title = escape(group_title(group, len(entries)))
            if section.title != title:
                section.title = title
            content = section.query_one("Contents")
            wanted = {entry["id"]: entry for entry in entries}
            rows = {row.entry["id"]: row for row in content.query(SessionRow)}
            for terminal_id, row in rows.items():
                if terminal_id not in wanted:
                    await row.remove()
            for terminal_id, entry in wanted.items():
                if terminal_id in rows:
                    rows[terminal_id].update_entry(entry)
                else:
                    await content.mount(SessionRow(entry))

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        name = event.button.id or ""
        if name.startswith("group-actions-"):
            self.app.push_screen(
                GroupActions(name[len("group-actions-") :], event.button.region)
            )
        elif name.startswith("open-session-"):
            await self.app.resume_session(name[len("open-session-") :])
        elif name.startswith("session-actions-"):
            terminal_id = name[len("session-actions-") :]
            self.app.push_screen(
                SessionActions(
                    self.app.snapshot["terminals"][terminal_id], event.button.region
                )
            )
        else:
            return
        event.stop()


class SessionsView(Horizontal):
    def __init__(self, snapshot: dict[str, Any]):
        super().__init__(id="sessions-page")
        self.snapshot = snapshot

    def compose(self) -> ComposeResult:
        with Vertical(id="sessions-side"):
            yield SessionList(self.snapshot)
            with Horizontal(id="sessions-footer"):
                yield Button("New group", id="new-group")
                yield Button("New session", id="new-session", variant="primary")
        yield TerminalPane()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "new-group":
            self.app.push_screen(GroupForm())
        elif event.button.id == "new-session":
            self.app.push_screen(NewSession())
        else:
            return
        event.stop()

    def on_terminal_pane_closed(self, event: TerminalPane.Closed) -> None:
        event.stop()
        self.mark_active(None)

    def mark_active(self, terminal_id: str | None) -> None:
        for row in self.query(SessionRow):
            row.set_class(row.entry["id"] == terminal_id, "active-session")

    async def on_mount(self) -> None:
        terminal_id = self.app.embedded_terminal
        if terminal_id in self.snapshot.get("terminals", {}):
            self.show_terminal(terminal_id)

    @property
    def can_embed(self) -> bool:
        return self.size.width >= EMBED_MIN_WIDTH

    def show_terminal(self, terminal_id: str) -> None:
        self.query_one(TerminalPane).open_terminal(terminal_id)
        self.mark_active(terminal_id)

    async def update_snapshot(self, snapshot: dict[str, Any]) -> None:
        self.snapshot = snapshot
        await self.query_one(SessionList).update_snapshot(snapshot)

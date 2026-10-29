from __future__ import annotations

from typing import Any
from rich.text import Text
from rich.markup import escape

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from .modal import BackdropModal
from textual.widgets import Button, Collapsible, Input, Select, Static, TextArea

from .options import HARNESS
from .terminal import TerminalView
from .session_rows import SessionRow, SessionSection, TaskSessions


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
            yield Select(
                HARNESS,
                value=self.app.snapshot["config"]["default_harness"],
                allow_blank=False,
                id="session-harness",
            )
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
        elif event.button.id == "session-start":
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
                self.dismiss(None)
                app.push_screen(TerminalView(result["terminal"]))
            except (ValueError, RuntimeError, OSError) as exc:
                self.app.notify(str(exc), severity="error")


class SessionActions(BackdropModal[None]):
    def __init__(self, entry: dict[str, Any]):
        super().__init__()
        self.entry = entry

    def compose(self) -> ComposeResult:
        running = self.entry["status"] in ("starting", "running")
        with Vertical(classes="sm"):
            yield Static("INDEPENDENT SESSION", classes="section-title")
            yield Input(self.entry["title"], id="session-title")
            yield Select(
                group_options(self.app.snapshot),
                value=self.entry.get("group", ""),
                allow_blank=False,
                id="session-group",
            )
            yield Static(
                "Restart resumes a known native ID; otherwise it starts fresh. Removing a record preserves vendor history and local logs.",
                classes="hint",
            )
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel", id="cancel")
                yield Button("Save", id="session-save", variant="primary")
            with Horizontal(classes="modal-actions"):
                yield Button("Start / Resume", id="session-restart", disabled=running)
                yield Button(
                    "Stop",
                    id="session-stop",
                    disabled=not running or not self.entry.get("attached"),
                )
                yield Button(
                    "Remove record",
                    id="session-remove",
                    disabled=running,
                    variant="error",
                )

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        name = event.button.id
        if name == "cancel":
            self.dismiss(None)
        elif name == "session-save":
            if await self.app.execute(
                {
                    "op": "session_update",
                    "terminal": self.entry["id"],
                    "title": self.query_one("#session-title", Input).value,
                    "group": self.query_one("#session-group", Select).value,
                }
            ):
                self.dismiss(None)
        elif name == "session-restart":
            try:
                result = await self.app.rpc(
                    {"op": "session_restart", "terminal": self.entry["id"]}
                )
                app = self.app
                await app.refresh_snapshot(force=True)
                self.dismiss(None)
                app.push_screen(TerminalView(result["terminal"]))
            except (ValueError, RuntimeError, OSError) as exc:
                self.app.notify(str(exc), severity="error")
        elif name in ("session-stop", "session-remove"):
            self.confirm_action(name)

    @work
    async def confirm_action(self, name: str) -> None:
        from .app import Confirm

        if await self.app.push_screen_wait(
            Confirm(
                "Stop this independent session?"
                if name == "session-stop"
                else "Remove this stopped session record?"
            )
        ):
            op = "terminal_stop" if name == "session-stop" else "session_delete"
            if await self.app.execute(
                {"op": op, "terminal": self.entry["id"], "confirmed": True}
            ):
                self.dismiss(None)


class SessionsView(VerticalScroll):
    def __init__(self, snapshot: dict[str, Any]):
        super().__init__(id="sessions-page")
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
            "": {"name": "Ungrouped"},
            **snapshot.get("custom", {}).get("groups", {}),
        }

    def group_section(self, group_id, group):
        key = "group-" + (group_id or "ungrouped")
        entries = self.entries(group_id)
        section = SessionSection(
            key,
            f"{group['name']} · {len(entries)}",
            self.app.preferences.is_collapsed(key),
        )
        if group_id:
            controls = Horizontal(classes="slot-row")
            controls.compose_add_child(
                Static(group["name"], classes="slot-name", markup=False)
            )
            controls.compose_add_child(Button("Rename", id="rename-group-" + group_id))
            controls.compose_add_child(Button("Remove", id="remove-group-" + group_id))
            section.compose_add_child(controls)
        for entry in entries:
            section.compose_add_child(SessionRow(entry))
        return section

    def compose(self) -> ComposeResult:
        with Horizontal(classes="toolbar"):
            yield Static("SESSIONS / NATIVE TERMINALS", classes="page-heading")
            yield Button("New group", id="new-group")
            yield Button("New session", id="new-session", variant="primary")
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
        yield Static(
            "Detach keeps sessions alive. Board Stop affects task terminals only. Independent session Stop always asks for confirmation.",
            classes="hint",
        )

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
                await self.mount(
                    self.group_section(group_id, group), before=self.query_one(".hint")
                )
                continue
            section = sections[0]
            entries = self.entries(group_id)
            title = escape(f"{group['name']} · {len(entries)}")
            if section.title != title:
                section.title = title
                if group_id:
                    section.query_one(".slot-name", Static).update(group["name"])
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
        if name == "new-group":
            self.app.push_screen(GroupForm())
        elif name == "new-session":
            self.app.push_screen(NewSession())
        elif name.startswith("rename-group-"):
            self.app.push_screen(GroupForm(name[len("rename-group-") :]))
        elif name.startswith("remove-group-"):
            self.remove_group(name[len("remove-group-") :])
        elif name.startswith("open-session-"):
            self.app.push_screen(TerminalView(name[len("open-session-") :]))
        elif name.startswith("session-actions-"):
            terminal_id = name[len("session-actions-") :]
            self.app.push_screen(
                SessionActions(self.app.snapshot["terminals"][terminal_id])
            )
        else:
            return
        event.stop()

    def remove_group(self, group_id: str) -> None:
        self.app.run_worker(self.confirm_group_removal(group_id))

    async def confirm_group_removal(self, group_id: str) -> None:
        from .app import Confirm

        app = self.app
        if await app.push_screen_wait(Confirm("Remove this empty session group?")):
            await app.execute(
                {"op": "group_delete", "group": group_id, "confirmed": True}
            )

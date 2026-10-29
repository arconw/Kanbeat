from __future__ import annotations

from typing import Any
from rich.text import Text

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from .modal import BackdropModal
from textual.widgets import Button, Collapsible, Input, Select, Static, TextArea

from .options import HARNESS
from .terminal import TerminalView


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

    def compose(self) -> ComposeResult:
        with Horizontal(classes="toolbar"):
            yield Static("SESSIONS / NATIVE TERMINALS", classes="page-heading")
            yield Button("New group", id="new-group")
            yield Button("New session", id="new-session", variant="primary")
        with Collapsible(title="Tasks", collapsed=True):
            for ticket in self.snapshot.get("tickets", []):
                state = ticket.get("state") or {}
                yield Static(
                    ticket["title"] + " · " + ticket["id"], classes="session-heading"
                )
                for slot in ["developer"] + [
                    row["id"] for row in state.get("reviewers", [])
                ]:
                    with Horizontal(classes="slot-row"):
                        yield Static(slot, classes="slot-name")
                        yield Button(
                            "Terminal",
                            id=f"term-{ticket['id']}-{slot}",
                            disabled=self.snapshot.get("execution") == "running",
                        )
                        yield Button("Logs", id=f"log-{ticket['id']}-{slot}")
        groups = {
            "": {"name": "Ungrouped"},
            **self.snapshot.get("custom", {}).get("groups", {}),
        }
        terminals = self.snapshot.get("terminals", {})
        for group_id, group in groups.items():
            with Horizontal(classes="slot-row"):
                yield Static(group["name"], classes="slot-name", markup=False)
                if group_id:
                    yield Button("Rename", id="rename-group-" + group_id)
                    yield Button("Remove", id="remove-group-" + group_id)
            for terminal_id, entry in terminals.items():
                if entry.get("owner") != "custom" or entry.get("group", "") != group_id:
                    continue
                with Horizontal(classes="slot-row"):
                    yield Static(
                        f"{entry['title']} / {entry['harness']} / {entry['status']}",
                        classes="slot-name",
                        markup=False,
                    )
                    yield Button(
                        "Open",
                        id="open-session-" + terminal_id,
                        disabled=not entry.get("attached"),
                    )
                    yield Button("Actions", id="session-actions-" + terminal_id)
        yield Static(
            "Detach keeps sessions alive. Board Stop affects task terminals only. Independent session Stop always asks for confirmation.",
            classes="hint",
        )

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

"""Mouse-first, keyboard-accessible Textual UI for TaskBeat.

FMODAL is edge-to-edge and borderless. SMODAL is centered and compact.
The embedded PTY terminal is experimental and an optional dependency.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import json
from typing import Any

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Collapsible, Input, Select, Static, TextArea

from ..dispatcher import Dispatcher
from ..sessions import TmuxSessions, SessionError
from ..storage import STATES, Ticket, TicketStore, atomic_write

HARNESS_OPTIONS = [
    ("Codex CLI", "codex"),
    ("Claude Code CLI", "claude"),
    ("OpenCode CLI", "opencode"),
    ("Pi CLI", "pi"),
]
STATUS_OPTIONS = [(name.replace("_", " ").title(), name) for name in STATES]


class _ComposedChildren:
    """Declarative tree assembly using public compose(), not Textual internals.

    This helper lets the page factory construct a widget hierarchy before mount.
    """
    def __init__(self, *args: Any, **kwargs: Any):
        self._declared_children: list[Any] = []
        super().__init__(*args, **kwargs)

    def add(self, widget: Any) -> None:
        self._declared_children.append(widget)

    def compose(self) -> ComposeResult:
        yield from self._declared_children


class PageVertical(_ComposedChildren, Vertical):
    pass


class PageHorizontal(_ComposedChildren, Horizontal):
    pass


class PageScroll(_ComposedChildren, VerticalScroll):
    pass


class ConfirmDialog(ModalScreen[bool]):
    def __init__(self, message: str):
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(classes="small-modal"):
            with Horizontal(classes="modal-header"):
                yield Static("Confirm action", classes="dialog-title")
                yield Button("×", id="cancel-top", variant="default")
            yield Static(self.message, classes="dialog-body")
            with Horizontal(classes="actions"):
                yield Button("Cancel", id="cancel")
                yield Button("Confirm", id="confirm", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")


class SessionModal(ModalScreen[None]):
    """A PTY client view of a tmux target, not ownership of its lifetime."""

    def __init__(self, sessions: TmuxSessions, target: str):
        super().__init__()
        self.sessions = sessions
        self.target = target

    def compose(self) -> ComposeResult:
        with Vertical(classes="full-modal"):
            with Horizontal(classes="modal-header"):
                yield Static("SESSION / " + self.target, classes="dialog-title")
                yield Button("× Close", id="close")
            try:
                from textual_tty import Terminal
            except ImportError:
                yield Static(
                    "Optional embedded terminal unavailable. Install: pip install 'arco9-taskbeat[terminal]'",
                    classes="hint",
                )
                yield VerticalScroll(Static(self.sessions.capture(self.target), id="session-capture"))
                yield Input(placeholder="Send a line to the existing tmux session", id="session-input")
                yield Button("Send", id="send")
            else:
                yield Terminal(
                    command=[
                        "tmux", "-S", str(self.sessions.socket),
                        "attach-session", "-t", "=" + self.target,
                    ],
                    id="embedded-terminal",
                )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close":
            self.dismiss(None)
        elif event.button.id == "send":
            self._send_line()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "session-input":
            self._send_line()

    def _send_line(self) -> None:
        value = self.query_one("#session-input", Input)
        try:
            self.sessions.send(self.target, value.value)
            value.value = ""
            self.query_one("#session-capture", Static).update(self.sessions.capture(self.target))
        except SessionError as exc:
            self.app.notify(str(exc), severity="error")


class LogModal(ModalScreen[None]):
    def __init__(self, sessions: TmuxSessions, target: str | None):
        super().__init__()
        self.sessions = sessions
        self.target = target

    def compose(self) -> ComposeResult:
        with Vertical(classes="small-modal log-modal"):
            with Horizontal(classes="modal-header"):
                yield Static("Recent harness output", classes="dialog-title")
                yield Button("×", id="close")
            with VerticalScroll(classes="log-window"):
                yield Static(
                    self.sessions.capture(self.target) if self.target else "No session attached",
                    id="log-content",
                )
            yield Button("Refresh", id="refresh")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close":
            self.dismiss(None)
        elif event.button.id == "refresh" and self.target:
            self.query_one("#log-content", Static).update(self.sessions.capture(self.target))


class ReviewerRow(Vertical):
    def __init__(self, item: dict[str, Any]):
        super().__init__(classes="reviewer-row")
        self.worker_id = str(item.get("id") or "reviewer")
        self.item = item

    def compose(self) -> ComposeResult:
        with Horizontal(classes="form-row"):
            yield Static(self.worker_id, classes="label-text")
            yield Button("Remove", id=None, classes="remove-reviewer")
        yield Select(HARNESS_OPTIONS, value=self.item.get("harness") or "codex")
        yield TextArea(str(self.item.get("cp") or ""), classes="prompt-editor")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.has_class("remove-reviewer"):
            self.remove()
            event.stop()

    def to_config(self) -> dict[str, Any]:
        return {
            "id": self.worker_id,
            "harness": self.query_one(Select).value,
            "cp": self.query_one(TextArea).text,
            "session": self.item.get("session"),
        }


class TicketModal(ModalScreen[bool]):
    def __init__(self, store: TicketStore, sessions: TmuxSessions, ticket: Ticket):
        super().__init__()
        self.store = store
        self.sessions = sessions
        self.ticket = ticket

    def compose(self) -> ComposeResult:
        with Vertical(classes="full-modal"):
            with Horizontal(classes="modal-header"):
                yield Static(f"TICKET / {self.ticket.id}", classes="dialog-title")
                yield Button("× Close", id="close")
            with VerticalScroll(classes="full-modal-content"):
                yield Static("TITLE", classes="field-label")
                yield Input(value=self.ticket.title, id="title")
                yield Static("DESCRIPTION / MARKDOWN", classes="field-label")
                yield TextArea(self.ticket.body, id="description")
                with Horizontal(classes="form-row"):
                    yield Static("Status", classes="field-label")
                    yield Select(STATUS_OPTIONS, value=self.ticket.status, id="status")
                    yield Button("Logs", id="logs")
                yield Static("DEVELOPER", classes="section-label")
                with Horizontal(classes="form-row"):
                    yield Static("Harness", classes="label-text")
                    yield Select(
                        HARNESS_OPTIONS,
                        value=self.ticket.developer.get("harness") or self.store.config()["default_harness"],
                        id="developer-harness",
                    )
                    yield Button("Open harness", id="developer-session")
                    yield Button("Reset", id="developer-reset")
                yield Static("CUSTOM PROMPT / DEVELOPER", classes="field-label")
                yield TextArea(str(self.ticket.developer.get("cp") or ""), id="developer-cp", classes="prompt-editor")
                yield Static("REVIEWERS / PARALLEL", classes="section-label")
                with Vertical(id="reviewers"):
                    for reviewer in self.ticket.reviewers:
                        yield ReviewerRow(reviewer)
                yield Button("+ Add reviewer", id="add-reviewer")
            with Horizontal(classes="actions"):
                yield Button("Cancel", id="cancel")
                yield Button("Save", id="save", variant="primary")

    def _save(self) -> None:
        rows = [row.to_config() for row in self.query(ReviewerRow)]
        if self.ticket.meta.get("review_iteration"):
            original = [r.get("id") for r in self.ticket.reviewers]
            if [r["id"] for r in rows] != original:
                self.app.notify(
                    "Reviewer roster is frozen during an active iteration", severity="error"
                )
                return
        self.ticket.meta["title"] = self.query_one("#title", Input).value.strip()
        self.ticket.body = self.query_one("#description", TextArea).text
        status = self.query_one("#status", Select).value
        if status in STATES:
            if self.ticket.meta.get("status") == "review" and status != "review":
                self.ticket.meta.pop("review_iteration", None)
            self.ticket.meta["status"] = status
        self.ticket.meta["developer"] = {
            **self.ticket.developer,
            "harness": self.query_one("#developer-harness", Select).value,
            "cp": self.query_one("#developer-cp", TextArea).text,
        }
        self.ticket.meta["reviewers"] = rows
        self.store.save(self.ticket)
        self.dismiss(True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        name = event.button.id
        if name in ("cancel", "close"):
            self.dismiss(False)
        elif name == "save":
            self._save()
        elif name == "add-reviewer":
            occupied = {row.worker_id for row in self.query(ReviewerRow)}
            number = 1
            while f"reviewer-{number}" in occupied:
                number += 1
            self.query_one("#reviewers", Vertical).mount(
                ReviewerRow({"id": f"reviewer-{number}", "harness": self.store.config()["default_harness"]})
            )
        elif name == "logs":
            session = self.ticket.developer.get("session")
            self.app.push_screen(LogModal(self.sessions, str(session) if session else None))
        elif name == "developer-session":
            target = self.ticket.developer.get("session")
            if target and self.sessions.is_alive(str(target)):
                self.app.push_screen(SessionModal(self.sessions, str(target)))
            else:
                self.app.notify("No live developer session for this ticket", severity="warning")
        elif name == "developer-reset":
            self.app.notify("Session reset and transcript handoff are scheduled for a later slice", severity="warning")


class TaskBeatApp(App[None]):
    TITLE = "Arco9 TaskBeat"
    CSS_PATH = "styles.tcss"
    BINDINGS = [
        ("1", "navigate('board')", "Board"),
        ("2", "navigate('sessions')", "Sessions"),
        ("3", "navigate('archive')", "Archive"),
        ("4", "navigate('settings')", "Settings"),
        ("ctrl+r", "reload", "Refresh"),
        ("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self, store: TicketStore):
        super().__init__()
        self.store = store
        self.sessions = TmuxSessions(store)
        self.dispatcher = Dispatcher(store, notify=self._engine_notify)
        self.tab = "board"
        self.select_mode = False
        self.selected: set[str] = set()
        self.actions: dict[str, tuple[str, str, str]] = {}
        self._action_number = 0
        self._snapshot: tuple[Any, ...] = ()
        self._last_error = ""

    def compose(self) -> ComposeResult:
        with Vertical(id="application"):
            with Horizontal(id="navigation"):
                yield Static("◉ ARCO9 / TASKBEAT", id="brand")
                for name in ("board", "sessions", "archive", "settings"):
                    yield Button(name.title(), id="nav-" + name, classes="nav-button")
            yield Container(id="viewport")
            yield Static(f"Project: {self.store.project}     [1-4] Tabs   [Ctrl+R] Refresh   [Ctrl+Q] Quit", id="statusbar")

    def on_mount(self) -> None:
        self.call_later(self.show_tab, "board")
        self.set_interval(1.5, self._poll)

    def _engine_notify(self, message: str, severity: str = "information") -> None:
        self.notify(message, severity=severity, timeout=8)

    def _poll(self) -> None:
        try:
            self.dispatcher.tick()
            rows = self.store.list()
            signature = tuple(
                (t.id, t.title, t.status, t.order, str(t.meta.get("review_iteration")))
                for t in rows
            )
            if signature != self._snapshot and self.tab in ("board", "sessions", "archive"):
                self._snapshot = signature
                self.call_later(self.show_tab, self.tab)
            self._last_error = ""
        except (OSError, ValueError, SessionError) as exc:
            if str(exc) != self._last_error:
                self._last_error = str(exc)
                self.notify(str(exc), severity="error", timeout=8)

    def _register(self, kind: str, ticket_id: str = "", extra: str = "") -> str:
        self._action_number += 1
        action_id = f"action-{self._action_number}"
        self.actions[action_id] = (kind, ticket_id, extra)
        return action_id

    def action_navigate(self, tab: str) -> None:
        self.call_later(self.show_tab, tab)

    def action_reload(self) -> None:
        self.call_later(self.show_tab, self.tab)

    async def show_tab(self, tab: str) -> None:
        self.tab = tab
        for name in ("board", "sessions", "archive", "settings"):
            self.query_one("#nav-" + name, Button).set_class(name == tab, "active")
        self.actions.clear()
        viewport = self.query_one("#viewport", Container)
        await viewport.remove_children()
        if tab == "board":
            await viewport.mount(self._board())
        elif tab == "sessions":
            await viewport.mount(self._session_page())
        elif tab == "archive":
            await viewport.mount(self._archive_page())
        else:
            await viewport.mount(self._settings_page())

    def _board(self) -> Vertical:
        root = PageVertical(id="board-page")
        bar = PageHorizontal(classes="page-toolbar")
        bar.add(Static("BOARD / PROJECT PIPELINE", classes="page-title"))
        bar.add(Button(
            "Exit select" if self.select_mode else "Select mode",
            id=self._register("select"),
        ))
        bar.add(Button("+ Ticket", id=self._register("new")))
        bar.add(Button("Archive", id=self._register("tab", "archive")))
        if self.select_mode:
            bar.add(Button("Bulk: Todo", id=self._register("bulk", "", "todo")))
            bar.add(Button("Bulk: Archive", id=self._register("bulk", "", "archive")))
        root.add(bar)
        lanes = PageHorizontal(id="lanes")
        rows = self.store.list()
        for status in ("backlog", "todo", "progress", "review", "done", "failed"):
            group = [t for t in rows if t.status == status]
            lane = PageScroll(classes="lane")
            lane.add(Static(f"{'REVIEW/QA' if status == 'review' else status.upper()}   {len(group):02d}", classes="lane-title"))
            for ticket in group:
                card = PageVertical(classes="ticket-card")
                with_title = PageHorizontal(classes="ticket-top")
                with_title.add(Button(
                    ticket.title[:52], id=self._register("open", ticket.id), classes="card-title"
                ))
                with_title.add(Button(
                    "···", id=self._register("actions", ticket.id), classes="card-menu"
                ))
                card.add(with_title)
                summary = ticket.body.strip().replace("\n", " ")
                card.add(Static(summary[:120] or "No description", classes="ticket-summary"))
                if self.select_mode:
                    checkbox = Checkbox("Select", value=ticket.id in self.selected)
                    checkbox.name = ticket.id
                    card.add(checkbox)
                elif ticket.status in ("progress", "review"):
                    card.add(Static("◌ AGENT ACTIVE", classes="state-running"))
                elif ticket.status == "failed":
                    card.add(Static("× FAILED", classes="state-failed"))
                lane.add(card)
            lanes.add(lane)
        root.add(lanes)
        return root

    def _session_page(self) -> VerticalScroll:
        page = PageScroll(id="session-page")
        page.add(Static("SESSIONS / PERSISTENT WORKSPACES", classes="page-title"))
        task_group = PageVertical(classes="session-section")
        for ticket in self.store.list():
            task_group.add(Static(f"{ticket.id} · {ticket.title}", classes="session-heading"))
            configs = [("developer", ticket.developer)] + [
                (str(r.get("id")), r) for r in ticket.reviewers
            ]
            for role, config in configs:
                target = config.get("session")
                if target:
                    task_group.add(
                        Button(
                            f"↗ {role}  ·  {config.get('harness', 'default')}  ·  {target[:12]}",
                            id=self._register("session", str(target)),
                            classes="session-button",
                        )
                    )
        page.add(Collapsible(task_group, title="Tasks (collapsed by default)", collapsed=True))
        page.add(Static("CUSTOM SESSIONS", classes="section-label"))
        row = PageHorizontal(classes="form-row")
        row.add(Input(placeholder="Session name", id="custom-name"))
        row.add(Input(value="My workspace", placeholder="Section", id="custom-group"))
        row.add(Select(HARNESS_OPTIONS, value="codex", id="custom-harness"))
        row.add(Button("+ Create", id=self._register("custom-session")))
        page.add(row)
        groups: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
        for target, info in self.sessions.load_registry().items():
            if not info.get("ticket_id"):
                groups[str(info.get("group") or "My workspace")].append((target, info))
        for group_name, entries in sorted(groups.items()):
            section = PageVertical(classes="session-section")
            section.add(Static(group_name, classes="session-heading"))
            for target, info in entries:
                buttons = PageHorizontal(classes="form-row")
                buttons.add(
                    Button(
                        "↗ " + str(info.get("title") or target) + " (" + str(info.get("harness")) + ")",
                        id=self._register("session", target),
                    )
                )
                buttons.add(Button("Stop", id=self._register("stop", target)))
                section.add(buttons)
            page.add(section)
        return page

    def _archive_page(self) -> VerticalScroll:
        page = PageScroll(id="archive-page")
        page.add(Static("ARCHIVE / OUT OF SCOPE", classes="page-title"))
        for ticket in self.store.list(archived=True):
            row = PageHorizontal(classes="archive-row")
            row.add(Static(ticket.title + " · " + ticket.id, classes="archive-title"))
            row.add(Button("Backlog", id=self._register("restore", ticket.id, "backlog")))
            row.add(Button("Todo", id=self._register("restore", ticket.id, "todo")))
            row.add(Button("Delete", id=self._register("delete", ticket.id), variant="error"))
            page.add(row)
        return page

    def _settings_page(self) -> VerticalScroll:
        config = self.store.config()
        page = PageScroll(id="settings-page")
        page.add(Static("SETTINGS / PROJECT DEFAULTS", classes="page-title"))
        page.add(Static("Default harness", classes="field-label"))
        page.add(Select(HARNESS_OPTIONS, value=config["default_harness"], id="default-harness"))
        for field, label in (
            ("global_cp", "Global CP"),
            ("developer_prompt", "Developer prompt"),
            ("reviewer_prompt", "Reviewer prompt"),
        ):
            page.add(Static(label, classes="field-label"))
            page.add(TextArea(str(config.get(field) or ""), id=field, classes="settings-editor"))
        page.add(Static("TRANSITION HOOK (CONFIGURED, EXECUTION PENDING)", classes="section-label"))
        hook = config.get("transition_hook", {})
        page.add(Select(
            [("Disabled", "")] + HARNESS_OPTIONS,
            value=hook.get("harness") or "", id="hook-harness",
        ))
        page.add(TextArea(str(hook.get("prompt") or ""), id="hook-prompt", classes="settings-editor"))
        page.add(Button("Save settings", id=self._register("save-settings"), variant="primary"))
        return page

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        ticket_id = event.checkbox.name
        if ticket_id:
            if event.value:
                self.selected.add(ticket_id)
            else:
                self.selected.discard(ticket_id)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id.startswith("nav-"):
            await self.show_tab(button_id[4:])
            return
        if button_id not in self.actions:
            return
        kind, ticket_id, extra = self.actions[button_id]
        try:
            if kind == "tab":
                await self.show_tab(ticket_id)
            elif kind == "select":
                self.select_mode = not self.select_mode
                self.selected.clear()
                await self.show_tab("board")
            elif kind == "new":
                ticket = self.store.create("New ticket")
                self.app.push_screen(
                    TicketModal(self.store, self.sessions, ticket),
                    callback=lambda _: self.call_later(self.show_tab, "board"),
                )
            elif kind == "open":
                self.app.push_screen(
                    TicketModal(self.store, self.sessions, self.store.read(ticket_id)),
                    callback=lambda _: self.call_later(self.show_tab, "board"),
                )
            elif kind == "actions":
                self.app.push_screen(
                    TicketActions(self.store, ticket_id),
                    callback=lambda _: self.call_later(self.show_tab, "board"),
                )
            elif kind == "bulk":
                for selected_id in tuple(self.selected):
                    if extra == "archive":
                        self.store.archive(selected_id)
                    else:
                        self.store.change_status(selected_id, extra, manual=True)
                self.selected.clear()
                self.select_mode = False
                await self.show_tab("board")
            elif kind == "restore":
                self.store.restore(ticket_id, extra)
                await self.show_tab("archive")
            elif kind == "delete":
                confirmed = await self.push_screen_wait(ConfirmDialog(
                    "Permanently delete archived ticket " + ticket_id + "?"
                ))
                if confirmed:
                    self.store.delete_archived(ticket_id)
                    await self.show_tab("archive")
            elif kind == "session":
                if self.sessions.is_alive(ticket_id):
                    self.push_screen(SessionModal(self.sessions, ticket_id))
                else:
                    self.notify("Session is offline; native resume is not yet implemented", severity="warning")
            elif kind == "stop":
                confirmed = await self.push_screen_wait(ConfirmDialog(
                    "Stop session " + ticket_id + "? This ends its native CLI process."
                ))
                if confirmed:
                    self.sessions.stop(ticket_id)
                    await self.show_tab("sessions")
            elif kind == "custom-session":
                name = self.query_one("#custom-name", Input).value.strip()
                group = self.query_one("#custom-group", Input).value.strip() or "My workspace"
                harness = self.query_one("#custom-harness", Select).value
                if name and isinstance(harness, str):
                    target = self.sessions.create_custom(name, harness, group)
                    self.push_screen(SessionModal(self.sessions, target))
            elif kind == "save-settings":
                config = self.store.config()
                config["default_harness"] = self.query_one("#default-harness", Select).value
                for field in ("global_cp", "developer_prompt", "reviewer_prompt"):
                    config[field] = self.query_one("#" + field, TextArea).text
                config["transition_hook"] = {
                    "harness": self.query_one("#hook-harness", Select).value,
                    "prompt": self.query_one("#hook-prompt", TextArea).text,
                }
                self.store.save_config(config)
                self.notify("Settings saved")
        except (OSError, ValueError, SessionError) as exc:
            self.notify(str(exc), severity="error")


class TicketActions(ModalScreen[None]):
    def __init__(self, store: TicketStore, ticket_id: str):
        super().__init__()
        self.store = store
        self.ticket_id = ticket_id

    def compose(self) -> ComposeResult:
        with Vertical(classes="small-modal"):
            with Horizontal(classes="modal-header"):
                yield Static("Ticket actions / " + self.ticket_id, classes="dialog-title")
                yield Button("×", id="close")
            yield Select(STATUS_OPTIONS, value=self.store.read(self.ticket_id).status, id="move-status")
            with Horizontal(classes="actions"):
                yield Button("Move", id="move", variant="primary")
                yield Button("Up", id="up")
                yield Button("Down", id="down")
                yield Button("Archive", id="archive", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        choice = event.button.id
        if choice == "close":
            self.dismiss(None)
            return
        try:
            if choice == "move":
                selected = self.query_one("#move-status", Select).value
                if selected in STATES:
                    self.store.change_status(self.ticket_id, selected, manual=True)
            elif choice in ("up", "down"):
                self.store.reorder(self.ticket_id, -1 if choice == "up" else 1)
            elif choice == "archive":
                self.store.archive(self.ticket_id)
            self.dismiss(None)
        except (OSError, ValueError) as exc:
            self.app.notify(str(exc), severity="error")

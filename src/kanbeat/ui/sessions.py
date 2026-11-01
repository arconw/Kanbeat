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

from .diff import DiffView
from .options import HARNESS, HARNESS_LABELS, SESSION_HARNESS
from .terminal import TerminalPane
from .session_rows import SessionRow, SessionSection, TaskSessions

EMBED_MIN_WIDTH = 114
SIDE_MIN_WIDTH = 34
DIFF_MIN_WIDTH = 44
TERMINAL_MIN_WIDTH = 60
SHELL = "shell"
SWITCH_BLOCKED_STATES = ("starting", "running", "stopping", "recovery_blocked")
SWITCH_EXCLUDED_ROLES = ("config", "hook")
SWITCH_TIMEOUT_MESSAGE = (
    "The harness switch timed out, so its outcome is unknown. Check the session "
    "card before retrying; the supervisor may still be switching."
)


def switch_eligible(entry: dict[str, Any]) -> bool:
    return (
        entry.get("owner") == "custom"
        and entry.get("harness") in HARNESS_LABELS
        and entry.get("role") not in SWITCH_EXCLUDED_ROLES
    )


def switch_locked(entry: dict[str, Any]) -> bool:
    return entry.get("status") in SWITCH_BLOCKED_STATES


def switch_tooltip(entry: dict[str, Any]) -> str:
    status = entry.get("status")
    if status in ("starting", "running"):
        return "Stop the session before switching its harness."
    if status == "recovery_blocked":
        return "Resolve orphan recovery before switching its harness."
    return "Wait until the session has stopped before switching its harness."


def switch_failure_message(error: BaseException) -> str:
    text = " ".join(str(error).split())
    for marker in (" The source ", " The active session "):
        index = text.find(marker)
        if index < 0:
            continue
        head = text[:index]
        colon = head.rfind(": ")
        if colon >= 0:
            text = head[:colon] + "." + text[index:]
        break
    return text


def harness_name(value: Any) -> str:
    return HARNESS_LABELS.get(value, str(value))


def byte_size(value: Any) -> str:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return ""
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.0f} KB"
    return f"{value / (1024 * 1024):.1f} MB"


def switch_question(entry: dict[str, Any], target: str, title: str) -> str:
    product = harness_name(target)
    name = escape(title or entry.get("title") or "this session")
    return (
        f"Switch {name} to {product}?\n\n"
        f"The whole conversation, including tool output, will be sent to "
        f"{product} and its provider.\n\n"
        "Permissions and settings are not transferred, and the new agent may "
        "behave differently."
    )


def switch_result_message(
    entry: dict[str, Any], target: str, result: dict[str, Any]
) -> str:
    name = entry.get("title") or "Session"
    product = harness_name(target)
    delivered = result.get("delivered")
    if isinstance(delivered, int) and not isinstance(delivered, bool) and delivered > 0:
        unit = "event" if delivered == 1 else "events"
        message = (
            f"Switched {name} to {product}; transferred {delivered} conversation "
            f"{unit}."
        )
    else:
        message = f"Switched {name} to {product}; there was no new conversation."
    unsupported = sorted(
        {str(item) for item in result.get("unsupported") or [] if str(item).strip()}
    )
    if unsupported:
        message += " Not transferred: " + ", ".join(unsupported) + "."
    return message


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
            yield Static("Native harness or a plain terminal", classes="field")
            with Horizontal(classes="field-row"):
                yield Select(
                    SESSION_HARNESS,
                    value=self.app.snapshot["config"]["default_harness"],
                    allow_blank=False,
                    id="session-harness",
                )
                yield Button("Open", id="open-harness")
            with Vertical(id="session-agent-fields"):
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

    @on(Select.Changed, "#session-harness")
    def show_harness_fields(self, event: Select.Changed) -> None:
        agent = event.value != SHELL
        self.query_one("#session-agent-fields").display = agent
        self.query_one("#open-harness", Button).display = agent

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
                harness = self.query_one("#session-harness", Select).value
                agent = harness != SHELL
                result = await self.app.rpc(
                    {
                        "op": "session_create",
                        "title": self.query_one("#session-title", Input).value,
                        "harness": harness,
                        "group": self.query_one("#session-group", Select).value,
                        "native_session": (
                            self.query_one("#session-native-id", Input).value
                            if agent
                            else ""
                        ),
                        "prompt": (
                            self.query_one("#session-prompt", TextArea).text
                            if agent
                            else ""
                        ),
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


class SwitchChoice(BackdropModal[dict[str, Any] | None]):
    BACKDROP_RESULT = None

    def __init__(
        self,
        title: str,
        message: str,
        options: list[tuple[str, dict[str, Any]]],
    ):
        super().__init__()
        self.title = title
        self.message = message
        self.options = options

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="sm"):
            with Horizontal(classes="modal-header"):
                yield Static(self.title, classes="modal-name")
                yield Button("×", id="no")
            yield Static(self.message, classes="modal-text", markup=False)
            for index, (label, _) in enumerate(self.options):
                yield Button(
                    label,
                    id=f"switch-choice-{index}",
                    classes="choice",
                    variant="primary",
                )
            with Horizontal(classes="modal-actions"):
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        name = event.button.id or ""
        if name in ("cancel", "no"):
            self.dismiss(None)
        elif name.startswith("switch-choice-"):
            try:
                index = int(name[len("switch-choice-") :])
            except ValueError:
                return
            if 0 <= index < len(self.options):
                self.dismiss(self.options[index][1])


class SessionEdit(BackdropModal[None]):
    def __init__(self, entry: dict[str, Any]):
        super().__init__()
        self.entry = entry
        self.saving = False
        self.finishing = False

    def dismiss(self, result: Any = None) -> Any:
        if self.saving and not self.finishing:
            self.app.notify(
                "Wait for the harness switch to finish.",
                severity="warning",
                markup=False,
            )
            return None
        return super().dismiss(result)

    def finish(self) -> None:
        self.finishing = True
        self.dismiss(None)

    def compose(self) -> ComposeResult:
        eligible = switch_eligible(self.entry)
        locked = eligible and switch_locked(self.entry)
        with Vertical(classes="sm"):
            yield Static("EDIT SESSION", classes="section-title")
            yield Input(self.entry["title"], id="session-title")
            yield Select(
                group_options(self.app.snapshot),
                value=self.entry.get("group", ""),
                allow_blank=False,
                id="session-group",
            )
            if eligible:
                yield Static("Harness", classes="field")
                yield Select(
                    HARNESS,
                    value=self.entry.get("harness"),
                    allow_blank=False,
                    id="session-harness",
                    disabled=locked,
                    tooltip=switch_tooltip(self.entry) if locked else None,
                )
            if self.entry.get("harness") != SHELL:
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
            self.save_changes()

    def current_entry(self) -> dict[str, Any]:
        return self.app.snapshot.get("terminals", {}).get(self.entry["id"], self.entry)

    def field_values(self) -> tuple[str, str, str]:
        title = self.query_one("#session-title", Input).value
        group = self.query_one("#session-group", Select).value
        agent = self.entry.get("harness") != SHELL
        native = (
            self.query_one("#session-native-id", Input).value.strip() if agent else ""
        )
        return title, group, native

    def chosen_harness(self) -> Any:
        if not switch_eligible(self.entry):
            return self.entry.get("harness")
        return self.query_one("#session-harness", Select).value

    async def apply_edits(self, title: str, group: Any, native: str) -> bool:
        try:
            await self.app.rpc(
                {
                    "op": "session_update",
                    "terminal": self.entry["id"],
                    "title": title,
                    "group": group,
                    "native_session": (
                        native if self.entry.get("harness") != SHELL else ""
                    ),
                }
            )
        except (RuntimeError, ValueError, OSError) as exc:
            self.app.notify(str(exc), severity="error", timeout=7, markup=False)
            return False
        await self.app.refresh_snapshot(force=True)
        return True

    @work
    async def save_changes(self) -> None:
        if self.saving:
            return
        self.saving = True
        save_button = self.query_one("#session-save", Button)
        cancel_button = self.query_one("#cancel", Button)
        save_button.disabled = True
        cancel_button.disabled = True
        try:
            await self._save_changes()
        finally:
            self.saving = False
            for button in (save_button, cancel_button):
                if button.is_mounted:
                    button.disabled = False

    async def _save_changes(self) -> None:
        title, group, native = self.field_values()
        source = self.entry.get("harness")
        target = self.chosen_harness()
        switching = switch_eligible(self.entry) and target != source
        if switching and switch_locked(self.current_entry()):
            self.app.notify(
                "Stop the session before switching its harness.",
                severity="warning",
                markup=False,
            )
            return
        changed = (
            title != self.entry.get("title", "")
            or group != self.entry.get("group", "")
            or (source != SHELL and native != (self.entry.get("native_session") or ""))
        )
        if switching:
            from .app import Confirm

            if not await self.app.push_screen_wait(
                Confirm(switch_question(self.entry, str(target), title))
            ):
                return
        if changed and not await self.apply_edits(title, group, native):
            return
        if changed:
            self.entry["title"] = title
            self.entry["group"] = group
            if source != SHELL:
                self.entry["native_session"] = native or None
        if not switching:
            self.finish()
            return
        await self.switch_to(str(target))

    async def switch_to(self, target: str) -> None:
        request: dict[str, Any] = {
            "op": "session_switch_harness",
            "terminal": self.entry["id"],
            "harness": target,
        }
        identifier = self.entry["id"]
        pending = getattr(self.app, "_pending_switches", None)
        if isinstance(pending, set) and identifier in pending:
            self.app.notify(
                "A harness switch is already in progress for this session.",
                severity="warning",
                markup=False,
            )
            return
        if isinstance(pending, set):
            pending.add(identifier)
        try:
            await self._switch_loop(request, target)
        finally:
            if isinstance(pending, set):
                pending.discard(identifier)

    async def _switch_loop(self, request: dict[str, Any], target: str) -> None:
        while True:
            try:
                result = await self.app.rpc(dict(request))
            except (RuntimeError, ValueError, OSError) as exc:
                if isinstance(exc, TimeoutError):
                    self.app.notify(
                        SWITCH_TIMEOUT_MESSAGE,
                        severity="error",
                        timeout=10,
                        markup=False,
                    )
                    return
                details = getattr(exc, "details", None)
                reason = details.get("reason") if isinstance(details, dict) else None
                if reason == "transfer_too_large":
                    choice = await self.app.push_screen_wait(
                        self.large_transfer_choice(exc, details)
                    )
                    if choice is None:
                        return
                    request["reduction"] = choice["reduction"]
                    continue
                if reason == "target_session_missing" and not request.get(
                    "fresh_session"
                ):
                    choice = await self.app.push_screen_wait(
                        self.missing_target_choice(target)
                    )
                    if choice is None:
                        return
                    request["fresh_session"] = True
                    continue
                self.app.notify(
                    switch_failure_message(exc),
                    severity="error",
                    timeout=10,
                    markup=False,
                )
                return
            await self.app.refresh_snapshot(force=True)
            self.app.notify(
                switch_result_message(self.entry, target, result),
                timeout=8,
                markup=False,
            )
            if self.is_mounted:
                self.finish()
            return

    def large_transfer_choice(
        self, error: BaseException, details: dict[str, Any]
    ) -> SwitchChoice:
        options: list[tuple[str, dict[str, Any]]] = []
        for row in details.get("options") or []:
            if not isinstance(row, dict):
                continue
            kind = row.get("kind")
            if kind == "newest":
                count = row.get("count")
                if isinstance(count, bool) or not isinstance(count, int):
                    continue
                reduction = {"kind": "newest", "count": count}
            elif kind == "text_only":
                reduction = {"kind": "text_only"}
            else:
                continue
            label = str(row.get("description") or kind)
            size = row.get("size")
            if isinstance(size, dict):
                text = byte_size(size.get("bytes"))
                if text:
                    label += f" ({text})"
            options.append((label, {"reduction": reduction}))
        message = str(error)
        unsupported = sorted(
            {
                str(item)
                for item in details.get("unsupported") or []
                if str(item).strip()
            }
        )
        if unsupported:
            message += (
                "\nNot transferred from the source: " + ", ".join(unsupported) + "."
            )
        return SwitchChoice("CONVERSATION TOO LARGE", message, options)

    def missing_target_choice(self, target: str) -> SwitchChoice:
        product = harness_name(target)
        message = (
            f"The saved {product} session no longer exists. Nothing is replaced "
            f"automatically. Start a new {product} session and transfer the "
            "accumulated conversation, or cancel."
        )
        return SwitchChoice(
            "TARGET SESSION MISSING",
            message,
            [(f"Start a fresh {product} session", {"fresh_session": True})],
        )


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
                yield Button(
                    "Remove from Kanbeat", id="session-remove", variant="error"
                )
                if self.entry.get("native_session") and self.entry["harness"] in (
                    "claude",
                    "codex",
                    "opencode",
                    "pi",
                ):
                    yield Button(
                        "Delete session and history",
                        id="session-remove-all",
                        variant="error",
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
        name = escape(self.entry["title"])
        product = HARNESS_LABELS.get(harness, harness)
        if native:
            question = (
                f"Delete {name} and its conversation in {product}? "
                "This cannot be undone."
            )
        elif harness == SHELL:
            question = f"Remove {name} from Kanbeat? Its shell is closed."
        else:
            question = (
                f"Remove {name} from Kanbeat? Its conversation stays in {product}."
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
        yield DiffView(self.app.project)

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
        self.app.apply_diff_layout()
        terminal_id = self.app.embedded_terminal
        if terminal_id in self.snapshot.get("terminals", {}):
            self.show_terminal(terminal_id)

    def diff_fits(self) -> bool:
        page = self.size.width
        side = max(SIDE_MIN_WIDTH, page * 20 // 100)
        panel = max(DIFF_MIN_WIDTH, page * 30 // 100)
        return page - side - panel >= TERMINAL_MIN_WIDTH

    def on_resize(self, event: events.Resize) -> None:
        self.app.apply_diff_layout()

    @property
    def can_embed(self) -> bool:
        return self.size.width >= EMBED_MIN_WIDTH

    def show_terminal(self, terminal_id: str) -> None:
        self.query_one(TerminalPane).open_terminal(terminal_id)
        self.mark_active(terminal_id)

    async def update_snapshot(self, snapshot: dict[str, Any]) -> None:
        self.snapshot = snapshot
        await self.query_one(SessionList).update_snapshot(snapshot)

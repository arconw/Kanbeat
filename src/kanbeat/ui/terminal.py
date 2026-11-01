from __future__ import annotations

import asyncio

from rich.segment import Segment
from rich.style import Style
from textual.strip import Strip
from textual import events, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.message import Message
from textual.widget import Widget
from .modal import BackdropModal
from textual.widgets import Button, Static
from bittty import Board
from textual_tty.monitor import Monitor

from .terminal_input import TerminalInput


READABLE_BLUES = {4: (95, 175, 255), 12: (135, 195, 255)}


class TerminalDisplay(Monitor):
    can_focus = True
    ALLOW_SELECT = False

    def __init__(self, *args, **kwargs):
        kwargs.setdefault(
            "board", Board(width=80, height=24, palette_overrides=READABLE_BLUES)
        )
        super().__init__(*args, **kwargs)
        self.drag_anchor: tuple[int, int] | None = None
        self.drag_head: tuple[int, int] | None = None
        self.dragging = False

    @property
    def session(self) -> TerminalSession:
        return next(
            node for node in self.ancestors if isinstance(node, TerminalSession)
        )

    def cell_at(self, event) -> tuple[int, int]:
        width, height = self.size
        return (
            max(0, min(int(event.x), width - 1)),
            max(0, min(int(event.y), height - 1)),
        )

    def selection_bounds(self) -> tuple[tuple[int, int], tuple[int, int]] | None:
        if self.drag_anchor is None or self.drag_head is None:
            return None
        if self.drag_anchor == self.drag_head:
            return None
        first, last = sorted(
            (self.drag_anchor, self.drag_head), key=lambda cell: (cell[1], cell[0])
        )
        return first, last

    def selected_span(self, y: int) -> tuple[int, int] | None:
        bounds = self.selection_bounds()
        if bounds is None:
            return None
        (first_x, first_y), (last_x, last_y) = bounds
        if not first_y <= y <= last_y:
            return None
        start = first_x if y == first_y else 0
        end = last_x + 1 if y == last_y else self.size.width
        return start, end

    def selected_text(self) -> str:
        bounds = self.selection_bounds()
        if bounds is None:
            return ""
        page = self.board.blitter.current_buffer
        (_, first_y), (_, last_y) = bounds
        lines = []
        for y in range(first_y, min(last_y, page.height - 1) + 1):
            start, end = self.selected_span(y)
            lines.append(page.get_line_text(y)[start:end].rstrip())
        return "\n".join(lines).strip("\n")

    def clear_selection(self) -> None:
        if self.dragging or self.drag_anchor is None:
            return
        self.drag_anchor = self.drag_head = None
        self.refresh()

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button != 1:
            return
        event.stop()
        event.prevent_default()
        self.focus()
        self.drag_anchor = self.drag_head = self.cell_at(event)
        self.dragging = True
        self.capture_mouse()
        self.refresh()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if not self.dragging:
            return
        event.stop()
        event.prevent_default()
        self.drag_head = self.cell_at(event)
        self.refresh()

    async def on_mouse_up(self, event: events.MouseUp) -> None:
        if not self.dragging:
            return
        event.stop()
        event.prevent_default()
        self.dragging = False
        self.release_mouse()
        self.drag_head = self.cell_at(event)
        text = self.selected_text()
        self.refresh()
        if text:
            await self.app.copy_selection(text)

    def render_line(self, y: int) -> Strip:
        strip = super().render_line(y)
        span = self.selected_span(y)
        if span is not None:
            width = self.size.width
            before, selected, after = strip.divide(
                [min(span[0], width), min(span[1], width), width]
            )
            selected = Strip(
                [
                    Segment(
                        segment.text,
                        (segment.style or Style()) + SELECTION_STYLE,
                        segment.control,
                    )
                    for segment in selected
                ],
                selected.cell_length,
            )
            strip = Strip.join([before, selected, after])
        return Strip(
            [
                Segment(segment.text, segment.style or Style(), segment.control)
                for segment in strip
            ],
            strip.cell_length,
        )

    async def on_key(self, event: events.Key) -> None:
        event.stop()
        event.prevent_default()
        self.clear_selection()
        await self.session.send_input(
            {"kind": "key", "key": event.key, "character": event.character}
        )

    async def on_paste(self, event: events.Paste) -> None:
        event.stop()
        await self.session.send_input({"kind": "paste", "text": event.text})

    async def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        event.stop()
        event.prevent_default()
        self.clear_selection()
        await self.session.scroll_terminal("up", event)

    async def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        event.stop()
        event.prevent_default()
        self.clear_selection()
        await self.session.scroll_terminal("down", event)

    async def on_resize(self, event: events.Resize) -> None:
        if event.size.width and event.size.height:
            await self.session.resize_terminal(event.size.height, event.size.width)


WAIT_SECONDS = 5
RETRY_SECONDS = 0.5
MAX_RETRY_SECONDS = 8
LIVE_STATUSES = ("starting", "running", "stopping")
ENDED_STATUSES = ("exited", "stopped", "failed", "interrupted")
SELECTION_STYLE = Style(color="#ffffff", bgcolor="#2f5f9e", bold=True)
NOT_RUNNING_ERROR = "Terminal is no longer running"


DEFAULT_HINT = "Ctrl+\\ Detach · Wheel scroll · Shift+wheel output history · Detach keeps it running"
MONITOR_HINT = (
    "Live worker monitor: input is disabled until this turn ends. "
    "Ctrl+\\ Detach · Wheel scroll · Shift+wheel output history"
)
DEFAULT_HEADING = "INTERACTIVE TERMINAL"


class TerminalSession:
    view_epoch = 0
    show_status = True

    def heading_text(self, text: str) -> str:
        return text

    def reset_terminal(self, terminal_id: str | None) -> None:
        self.terminal_id = terminal_id
        self.revision = -1
        self.heading = None
        self.rows_text: list[str] = []
        self.cursor_state = None
        self.grid = None
        self.input = TerminalInput(self.rpc, self.report_input_error)
        self.scroll_position = None
        self.view_epoch += 1
        self.last_snapshot = {}
        self.read_only = False
        self.read_only_notified = False
        self.was_live = False

    @property
    def controls_root(self) -> Widget:
        return self

    def part(self, selector: str, kind: type[Widget]) -> Widget | None:
        try:
            return self.controls_root.query_one(selector, kind)
        except NoMatches:
            return None

    def local(self, selector: str) -> Widget | None:
        try:
            return self.query_one(selector)
        except NoMatches:
            return None

    def display(self) -> TerminalDisplay | None:
        try:
            return self.query_one(TerminalDisplay)
        except NoMatches:
            return None

    def show_hint(self, text: str) -> None:
        raise NotImplementedError

    async def press_control(self, name: str) -> None:
        if name == "terminal-detach":
            await self.action_detach()
        elif name == "terminal-live":
            await self.go_live()
        elif name == "terminal-stop":
            await self.stop_terminal()

    def begin(self) -> None:
        display = self.display()
        if display is not None:
            display.focus()
        self.watch_terminal()
        self.pump_input()

    def leave(self) -> None:
        raise NotImplementedError

    async def rpc(self, op: str, **values):
        return await self.app.rpc({"op": op, "terminal": self.terminal_id, **values})

    @work(exclusive=True, group="terminal-watch", exit_on_error=False)
    async def watch_terminal(self) -> None:
        failed = False
        retry = RETRY_SECONDS
        try:
            while True:
                try:
                    epoch = self.view_epoch
                    if self.revision < 0:
                        snapshot = await self.rpc(
                            "terminal_snapshot", history_position=self.scroll_position
                        )
                    else:
                        snapshot = await self.rpc(
                            "terminal_wait",
                            revision=self.revision,
                            timeout=WAIT_SECONDS,
                            history_position=self.scroll_position,
                        )
                    if epoch == self.view_epoch:
                        applied = self.apply_snapshot(snapshot)
                        while not applied and epoch == self.view_epoch:
                            if not self.is_attached or self.terminal_id is None:
                                return
                            await asyncio.sleep(RETRY_SECONDS)
                            applied = self.apply_snapshot(snapshot)
                        if not applied:
                            continue
                    if self.was_live and snapshot["status"] in ENDED_STATUSES:
                        self.detach_ended(snapshot)
                        return
                    if self.input.error and snapshot["status"] == "running":
                        self.input = TerminalInput(self.rpc, self.report_input_error)
                        self.pump_input()
                        self.app.notify(
                            "Terminal input reconnected; previous delivery is unconfirmed",
                            severity="warning",
                        )
                    failed = False
                    retry = RETRY_SECONDS
                except (OSError, RuntimeError, ValueError) as exc:
                    if not self.is_attached or self.terminal_id is None:
                        return
                    if not failed:
                        self.app.notify(str(exc), severity="error")
                    reason = str(exc).lower()
                    heading = "TERMINAL DISCONNECTED · " + str(exc)[:80]
                    if heading != self.heading:
                        heading_widget = self.part("#terminal-heading", Static)
                        if heading_widget is not None:
                            self.heading = heading
                            heading_widget.update(self.heading_text(heading))
                    if (
                        "not attached" in reason
                        or "no longer attached" in reason
                        or "not found" in reason
                        or "shutting down" in reason
                    ):
                        return
                    failed = True
                    await asyncio.sleep(retry)
                    retry = min(retry * 2, MAX_RETRY_SECONDS)
        except asyncio.CancelledError:
            raise
        except NoMatches:
            return
        except Exception as exc:
            if not self.is_attached or self.terminal_id is None:
                return
            self.app.notify(str(exc), severity="error")

    def apply_snapshot(self, snapshot: dict) -> bool:
        display = self.display()
        if display is None:
            return False
        self.revision = snapshot["revision"]
        self.last_snapshot = snapshot
        self.was_live = self.was_live or snapshot["status"] in LIVE_STATUSES
        read_only = bool(snapshot.get("read_only"))
        if read_only != self.read_only:
            self.read_only = read_only
            self.read_only_notified = False
            self.show_hint(MONITOR_HINT if read_only else DEFAULT_HINT)
        if self.scroll_position is not None:
            self.scroll_position = (
                snapshot["scroll_position"] if snapshot["historical"] else None
            )
        live = self.part("#terminal-live", Button)
        if live is not None:
            live.disabled = self.scroll_position is None
        title = snapshot["title"]
        if self.show_status:
            title += " / " + snapshot["status"]
        if snapshot.get("dropped_replies"):
            title += f" / {snapshot['dropped_replies']} terminal replies dropped"
        if snapshot["historical"]:
            title += " / output history"
        if snapshot.get("read_only"):
            title += " / monitor"
        if title != self.heading:
            heading = self.part("#terminal-heading", Static)
            if heading is not None:
                self.heading = title
                heading.update(self.heading_text(title))
        size = (snapshot["cols"], snapshot["rows"])
        lines = snapshot["screen"].split("\n")
        if size != self.grid:
            self.grid = size
            display.board.resize(*size)
            self.rows_text = []
        content = ""
        for row, line in enumerate(lines, 1):
            if row > len(self.rows_text) or self.rows_text[row - 1] != line:
                content += f"\x1b[{row};1H" + line + "\x1b[0m"
        cursor = (
            snapshot["cursor"][0],
            snapshot["cursor"][1],
            snapshot["cursor_visible"],
        )
        if content or cursor != self.cursor_state:
            x, y, visible = cursor
            content = "\x1b[?7l" + content + f"\x1b[{y + 1};{x + 1}H"
            content += "\x1b[?25h" if visible else "\x1b[?25l"
            display.feed(content)
            display.clear_selection()
        self.rows_text = lines
        self.cursor_state = cursor
        return True

    def detach_ended(self, snapshot: dict) -> None:
        message = f"{snapshot['title']} {snapshot['status']}"
        if snapshot.get("exit_code") is not None:
            message += f" (exit {snapshot['exit_code']})"
        self.app.notify(message + "; detached")
        self.leave()

    def report_input_error(self, message: str) -> None:
        if self.was_live and message.endswith(NOT_RUNNING_ERROR):
            return
        self.app.notify(message, severity="error")

    async def send_input(self, values: dict) -> None:
        if self.read_only:
            if not self.read_only_notified:
                self.read_only_notified = True
                self.app.notify(
                    "Live worker monitor is read-only until the turn ends",
                    severity="warning",
                )
            return
        if self.scroll_position is not None and values.get("kind") != "wheel":
            await self.go_live()
        try:
            await self.input.put(values)
        except RuntimeError as exc:
            self.report_input_error(str(exc))

    @work(exclusive=True, group="terminal-input")
    async def pump_input(self) -> None:
        await self.input.run()

    async def refresh_view(self) -> None:
        self.view_epoch += 1
        epoch = self.view_epoch
        try:
            snapshot = await self.rpc(
                "terminal_snapshot", history_position=self.scroll_position
            )
            if epoch == self.view_epoch:
                self.apply_snapshot(snapshot)
        except (OSError, ValueError, RuntimeError) as exc:
            self.report_input_error(str(exc))

    async def go_live(self) -> None:
        self.scroll_position = None
        await self.refresh_view()
        display = self.display()
        if display is not None:
            display.focus()

    async def scroll_terminal(self, direction: str, event) -> None:
        snapshot = self.last_snapshot
        if not snapshot:
            return
        if self.scroll_position is None:
            if not event.shift and await self.forward_wheel(direction, event):
                return
            if (
                event.shift
                and self.can_forward_wheel(snapshot)
                and not self.has_local_history(snapshot)
                and (self.read_only or snapshot.get("owner") == "attach")
                and await self.forward_wheel(direction, event)
            ):
                return
        current = (
            self.scroll_position
            if self.scroll_position is not None
            else snapshot["history_total"]
        )
        self.scroll_position = min(
            snapshot["history_total"],
            max(snapshot["history_start"], current + (-3 if direction == "up" else 3)),
        )
        await self.refresh_view()

    @staticmethod
    def has_local_history(snapshot: dict) -> bool:
        return snapshot["history_total"] > snapshot["history_start"]

    @staticmethod
    def can_forward_wheel(snapshot: dict) -> bool:
        return bool(
            snapshot.get("mouse_tracking")
            or snapshot.get("alternate_scroll")
            or snapshot.get("alternate_screen")
        )

    async def forward_wheel(self, direction: str, event) -> bool:
        snapshot = self.last_snapshot
        if not self.can_forward_wheel(snapshot):
            return False
        if self.read_only:
            return await self.navigate_monitor(direction, event)
        if snapshot.get("mouse_tracking") or snapshot.get("alternate_scroll"):
            await self.send_input(
                {
                    "kind": "wheel",
                    "direction": direction,
                    "x": int(event.x) + 1,
                    "y": int(event.y) + 1,
                    "modifiers": [
                        key for key in ("meta", "ctrl") if getattr(event, key)
                    ],
                }
            )
            return True
        await self.send_input(
            {"kind": "key", "key": "pageup" if direction == "up" else "pagedown"}
        )
        return True

    async def navigate_monitor(self, direction: str, event) -> bool:
        self.view_epoch += 1
        epoch = self.view_epoch
        try:
            snapshot = await self.rpc(
                "terminal_navigate",
                direction=direction,
                x=int(event.x) + 1,
                y=int(event.y) + 1,
                modifiers=[key for key in ("meta", "ctrl") if getattr(event, key)],
            )
            if epoch == self.view_epoch:
                self.apply_snapshot(snapshot)
            return True
        except (OSError, RuntimeError, ValueError) as exc:
            self.report_input_error(str(exc))
            return False

    async def resize_terminal(self, rows: int, cols: int) -> None:
        try:
            await self.rpc("terminal_resize", rows=min(rows, 100), cols=min(cols, 240))
        except (OSError, RuntimeError, ValueError) as exc:
            self.app.notify(str(exc), severity="error")

    async def action_detach(self) -> None:
        try:
            await self.input.drain()
        except RuntimeError as exc:
            self.report_input_error(str(exc))
        self.leave()

    async def stop_terminal(self) -> None:
        try:
            await self.rpc("terminal_stop", confirmed=True)
        except (OSError, RuntimeError, ValueError) as exc:
            self.app.notify(str(exc), severity="error")


class TerminalView(TerminalSession, BackdropModal[None]):
    BINDINGS = [Binding("ctrl+backslash", "detach", "Detach", priority=True)]

    def __init__(self, terminal_id: str):
        super().__init__()
        self.reset_terminal(terminal_id)

    def compose(self) -> ComposeResult:
        with Vertical(classes="full"):
            with Horizontal(classes="modal-header"):
                yield Static(
                    DEFAULT_HEADING, id="terminal-heading", classes="modal-name"
                )
                yield Button("Live", id="terminal-live", disabled=True)
                yield Button("Stop", id="terminal-stop", variant="error")
                yield Button("Detach", id="terminal-detach")
            yield Static(DEFAULT_HINT, classes="terminal-hint", id="terminal-hint")
            yield TerminalDisplay(id="terminal-display")

    def show_hint(self, text: str) -> None:
        hint = self.part("#terminal-hint", Static)
        if hint is not None:
            hint.update(text)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        await self.press_control(event.button.id or "")

    async def on_mount(self) -> None:
        self.begin()

    def leave(self) -> None:
        self.dismiss(None)


class TerminalPane(TerminalSession, Vertical):
    BINDINGS = [Binding("ctrl+backslash", "detach", "Detach", priority=True)]
    show_status = False

    class Closed(Message):
        pass

    def __init__(self):
        super().__init__(id="terminal-pane")
        self.reset_terminal(None)

    def compose(self) -> ComposeResult:
        yield Static(
            "Click a session to open its terminal here",
            id="terminal-empty",
        )
        with Vertical(id="terminal-body"):
            yield TerminalDisplay(id="terminal-display")

    @property
    def controls_root(self) -> Widget:
        stack = self.app.screen_stack
        return stack[0] if stack else self.screen

    def heading_text(self, text: str) -> str:
        return "| " + text if text else ""

    def show_hint(self, text: str) -> None:
        button = self.part("#terminal-detach", Button)
        if button is not None:
            button.tooltip = text

    def show_controls(self, visible: bool) -> None:
        controls = self.part("#terminal-controls", Widget)
        if controls is not None:
            controls.display = visible

    async def on_mount(self) -> None:
        self.show_body(False)

    def on_unmount(self) -> None:
        self.workers.cancel_group(self, "terminal-watch")
        self.workers.cancel_group(self, "terminal-input")
        self.show_controls(False)
        heading = self.part("#terminal-heading", Static)
        if heading is not None:
            heading.update("")

    def show_body(self, visible: bool) -> None:
        self.show_controls(visible)
        body = self.local("#terminal-body")
        if body is not None:
            body.display = visible
        empty = self.local("#terminal-empty")
        if empty is not None:
            empty.display = not visible

    def open_terminal(self, terminal_id: str) -> None:
        self.workers.cancel_group(self, "terminal-watch")
        self.reset_terminal(terminal_id)
        self.app.embedded_terminal = terminal_id
        self.app.update_diff_button()
        heading = self.part("#terminal-heading", Static)
        if heading is not None:
            heading.update("")
        self.show_hint(DEFAULT_HINT)
        live = self.part("#terminal-live", Button)
        if live is not None:
            live.disabled = True
        display = self.display()
        if display is not None:
            display.feed("\x1b[2J\x1b[H")
        self.show_body(True)
        self.begin()
        self.call_after_refresh(self.fit_terminal)

    async def fit_terminal(self) -> None:
        display = self.display()
        if display is None:
            return
        size = display.size
        if size.width and size.height and self.terminal_id is not None:
            await self.resize_terminal(size.height, size.width)

    def leave(self) -> None:
        self.workers.cancel_group(self, "terminal-watch")
        self.workers.cancel_group(self, "terminal-input")
        self.reset_terminal(None)
        self.app.embedded_terminal = None
        self.app.update_diff_button()
        heading = self.part("#terminal-heading", Static)
        if heading is not None:
            heading.update("")
        self.show_body(False)
        self.post_message(self.Closed())

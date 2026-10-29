from __future__ import annotations

import asyncio

from rich.segment import Segment
from rich.style import Style
from textual.strip import Strip
from textual import events, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from .modal import BackdropModal
from textual.widgets import Button, Static
from textual_tty.monitor import Monitor

from .terminal_input import TerminalInput


class TerminalDisplay(Monitor):
    can_focus = True

    def render_line(self, y: int) -> Strip:
        strip = super().render_line(y)
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
        await self.screen.send_input(
            {"kind": "key", "key": event.key, "character": event.character}
        )

    async def on_paste(self, event: events.Paste) -> None:
        event.stop()
        await self.screen.send_input({"kind": "paste", "text": event.text})

    async def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        event.stop()
        event.prevent_default()
        await self.screen.scroll_terminal("up", event)

    async def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        event.stop()
        event.prevent_default()
        await self.screen.scroll_terminal("down", event)

    async def on_resize(self, event: events.Resize) -> None:
        if event.size.width and event.size.height:
            await self.screen.resize_terminal(event.size.height, event.size.width)


WAIT_SECONDS = 5
RETRY_SECONDS = 0.5


class TerminalView(BackdropModal[None]):
    BINDINGS = [Binding("ctrl+backslash", "detach", "Detach", priority=True)]

    def __init__(self, terminal_id: str):
        super().__init__()
        self.terminal_id = terminal_id
        self.revision = -1
        self.heading = None
        self.rows_text: list[str] = []
        self.cursor_state = None
        self.grid = None
        self.input = TerminalInput(self.rpc, self.report_input_error)
        self.scroll_position = None
        self.view_epoch = 0
        self.last_snapshot = {}

    def compose(self) -> ComposeResult:
        with Vertical(classes="full"):
            with Horizontal(classes="modal-header"):
                yield Static(
                    "INTERACTIVE TERMINAL", id="terminal-heading", classes="modal-name"
                )
                yield Button("Live", id="terminal-live", disabled=True)
                yield Button("Stop", id="terminal-stop", variant="error")
                yield Button("Detach", id="terminal-detach")
            yield Static(
                "Ctrl+\\ Detach · Wheel scroll · Shift+wheel output history · Detach keeps it running",
                classes="terminal-hint",
            )
            yield TerminalDisplay(id="terminal-display")

    async def on_mount(self) -> None:
        self.query_one(TerminalDisplay).focus()
        self.watch_terminal()
        self.pump_input()

    async def rpc(self, op: str, **values):
        return await self.app.rpc({"op": op, "terminal": self.terminal_id, **values})

    @work(exclusive=True, group="terminal-watch")
    async def watch_terminal(self) -> None:
        failed = False
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
                    self.apply_snapshot(snapshot)
                failed = False
            except (OSError, RuntimeError, ValueError) as exc:
                if not failed:
                    self.app.notify(str(exc), severity="error")
                failed = True
                await asyncio.sleep(RETRY_SECONDS)

    def apply_snapshot(self, snapshot: dict) -> None:
        self.revision = snapshot["revision"]
        self.last_snapshot = snapshot
        if self.scroll_position is not None:
            self.scroll_position = (
                snapshot["scroll_position"] if snapshot["historical"] else None
            )
        self.query_one("#terminal-live", Button).disabled = self.scroll_position is None
        title = f"{snapshot['title']} / {snapshot['status']}"
        if snapshot["historical"]:
            title += " / output history"
        if title != self.heading:
            self.heading = title
            self.query_one("#terminal-heading", Static).update(title)
        display = self.query_one(TerminalDisplay)
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
        self.rows_text = lines
        self.cursor_state = cursor

    def report_input_error(self, message: str) -> None:
        self.app.notify(message, severity="error")

    async def send_input(self, values: dict) -> None:
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
        self.query_one(TerminalDisplay).focus()

    async def scroll_terminal(self, direction: str, event) -> None:
        snapshot = self.last_snapshot
        if not snapshot:
            return
        if not event.shift and self.scroll_position is None:
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
                return
            if snapshot.get("alternate_screen"):
                await self.send_input(
                    {
                        "kind": "key",
                        "key": "pageup" if direction == "up" else "pagedown",
                    }
                )
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
        self.dismiss(None)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "terminal-detach":
            await self.action_detach()
        elif event.button.id == "terminal-live":
            await self.go_live()
        elif event.button.id == "terminal-stop":
            self.confirm_stop()

    @work
    async def confirm_stop(self) -> None:
        from .app import Confirm

        if await self.app.push_screen_wait(Confirm("Stop this interactive session?")):
            await self.rpc("terminal_stop", confirmed=True)

from __future__ import annotations

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

    async def on_resize(self, event: events.Resize) -> None:
        if event.size.width and event.size.height:
            await self.screen.resize_terminal(event.size.height, event.size.width)


class TerminalView(BackdropModal[None]):
    BINDINGS = [Binding("ctrl+backslash", "detach", "Detach", priority=True)]

    def __init__(self, terminal_id: str):
        super().__init__()
        self.terminal_id = terminal_id
        self.polling = False
        self.last_screen = None

    def compose(self) -> ComposeResult:
        with Vertical(classes="full"):
            with Horizontal(classes="modal-header"):
                yield Static(
                    "INTERACTIVE TERMINAL", id="terminal-heading", classes="modal-name"
                )
                yield Button("Stop", id="terminal-stop", variant="error")
                yield Button("Detach", id="terminal-detach")
            yield TerminalDisplay(id="terminal-display")
            yield Static(
                "Ctrl+\\ detaches. Closing this view keeps the process running.",
                classes="hint",
            )

    async def on_mount(self) -> None:
        self.query_one(TerminalDisplay).focus()
        await self.refresh_terminal()
        self.set_interval(0.15, self.refresh_terminal)

    async def rpc(self, op: str, **values):
        return await self.app.rpc({"op": op, "terminal": self.terminal_id, **values})

    async def refresh_terminal(self) -> None:
        if self.polling:
            return
        self.polling = True
        try:
            snapshot = await self.rpc("terminal_snapshot")
            self.query_one("#terminal-heading", Static).update(
                f"{snapshot['title']} / {snapshot['status']}"
            )
            if snapshot != self.last_screen:
                display = self.query_one(TerminalDisplay)
                display.board.resize(snapshot["cols"], snapshot["rows"])
                content = "\x1b[?7l"
                for row, line in enumerate(snapshot["screen"].split("\n"), 1):
                    content += f"\x1b[{row};1H" + line + "\x1b[0m"
                x, y = snapshot["cursor"]
                content += f"\x1b[{y + 1};{x + 1}H"
                content += "\x1b[?25h" if snapshot["cursor_visible"] else "\x1b[?25l"
                display.feed(content)
                self.last_screen = snapshot
        except (OSError, RuntimeError, ValueError) as exc:
            self.app.notify(str(exc), severity="error")
        finally:
            self.polling = False

    async def send_input(self, values: dict) -> None:
        try:
            await self.rpc("terminal_input", **values)
            await self.refresh_terminal()
        except (OSError, RuntimeError, ValueError) as exc:
            self.app.notify(str(exc), severity="error")

    async def resize_terminal(self, rows: int, cols: int) -> None:
        try:
            await self.rpc("terminal_resize", rows=min(rows, 100), cols=min(cols, 240))
        except (OSError, RuntimeError, ValueError) as exc:
            self.app.notify(str(exc), severity="error")

    def action_detach(self) -> None:
        self.dismiss(None)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "terminal-detach":
            self.dismiss(None)
        elif event.button.id == "terminal-stop":
            self.confirm_stop()

    @work
    async def confirm_stop(self) -> None:
        from .app import Confirm

        if await self.app.push_screen_wait(Confirm("Stop this interactive session?")):
            await self.rpc("terminal_stop", confirmed=True)
            await self.refresh_terminal()

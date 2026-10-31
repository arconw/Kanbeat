from __future__ import annotations

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Center, Middle
from textual.screen import Screen
from textual.widgets import Static


class StartupScreen(Screen):
    def on_key(self, event: events.Key) -> None:
        if event.key == "ctrl+q":
            return
        event.stop()
        event.prevent_default()

    def compose(self) -> ComposeResult:
        wordmark = Text()
        wordmark.append("Kan", style="bold #7ee5d5")
        wordmark.append("Beat", style="bold #b0a0ff")
        with Middle():
            with Center():
                yield Static(wordmark, id="startup-wordmark")
            with Center():
                yield Static("Local tasks · native harnesses", id="startup-subtitle")

from __future__ import annotations

from typing import TypeVar

from textual import events
from textual.screen import ModalScreen

Result = TypeVar("Result")


class BackdropModal(ModalScreen[Result]):
    BACKDROP_RESULT = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._backdrop_pressed = False

    def on_mouse_down(self, event: events.MouseDown) -> None:
        self._backdrop_pressed = event.button == 1 and event.widget is self
        if self._backdrop_pressed:
            event.stop()

    def on_click(self, event: events.Click) -> None:
        if event.widget is self:
            event.stop()
            event.prevent_default()
            if event.button == 1 and self._backdrop_pressed:
                self._backdrop_pressed = False
                self.dismiss(self.BACKDROP_RESULT)

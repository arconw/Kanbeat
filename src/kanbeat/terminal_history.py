from __future__ import annotations

from collections import deque

from bittty import Board
from rich.cells import set_cell_size

HISTORY_LINES = 2000


class HistoryBoard(Board):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.history: deque[str] = deque(maxlen=HISTORY_LINES)
        self.history_total = 0
        self._native_scroll = self.blitter.scroll
        self.blitter.scroll = self.retain_scrolled_lines

    def retain_scrolled_lines(self, lines: int) -> None:
        blitter = self.blitter
        if (
            lines > 0
            and not blitter.in_alt_screen
            and blitter.scroll_top == 0
            and blitter.scroll_bottom == self.height - 1
            and blitter.left_margin == 0
            and blitter.right_margin == self.width - 1
        ):
            count = min(lines, self.height)
            self.history.extend(
                blitter.current_buffer.get_line_text(row) for row in range(count)
            )
            self.history_total += count
        self._native_scroll(lines)

    @property
    def history_start(self) -> int:
        return self.history_total - len(self.history)

    def history_window(self, position: int) -> tuple[int, str]:
        position = min(max(position, self.history_start), self.history_total)
        lines = list(self.history) + self.capture_text(trim=False).split("\n")
        start = position - self.history_start
        window = lines[start : start + self.height]
        window += [""] * (self.height - len(window))
        return position, "\n".join(set_cell_size(line, self.width) for line in window)

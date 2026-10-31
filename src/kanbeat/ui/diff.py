from __future__ import annotations

import asyncio
from pathlib import Path

from rich.style import Style
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.geometry import Size
from textual.message import Message
from textual.scroll_view import ScrollView
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Button, Static

from ..diff import SOURCES, Changes, FileDiff, collect, is_generated
from .diff_render import (
    ADD_COLOR,
    REMOVE_COLOR,
    FileRender,
    Row,
    group_row,
    header_row,
    list_row,
    message_row,
    render_files,
    rule_row,
    wrapped_rows,
)

LOADING = "Loading changes…"
NOT_A_REPOSITORY = (
    "This project is not inside a Git repository, or Git is not installed."
)
CLEAN = "No changes."
EMPTY_SOURCE = "Nothing to show for this source."
FILES_HEIGHT_SHARE = 3


def summary_markup(changes: Changes | None, compact: bool = False) -> str:
    if changes is None or not changes.available or not changes.files:
        return "files"
    count = len(changes.files)
    label = str(count) if compact else f"{count} {'file' if count == 1 else 'files'}"
    separator = " " if compact else " | "
    return (
        f"{label}{separator}[{ADD_COLOR}]+{changes.additions}[/] "
        f"[{REMOVE_COLOR}]-{changes.deletions}[/]"
    )


def summary_tooltip(changes: Changes | None) -> str:
    if changes is None or not changes.available:
        return "Show the project's changes"
    count = len(changes.files)
    if not count:
        return "No changes in the working tree"
    return (
        f"{count} changed {'file' if count == 1 else 'files'}: "
        f"{changes.additions} added, {changes.deletions} removed lines"
    )


def heading_markup(changes: Changes | None) -> str:
    if changes is None:
        return LOADING
    if not changes.files:
        return "No changes"
    count = len(changes.files)
    noun = "file" if count == 1 else "files"
    return (
        f"[b]{count} {noun}[/b] changed [{ADD_COLOR}]+{changes.additions}[/] "
        f"[{REMOVE_COLOR}]-{changes.deletions}[/]"
    )


class RowView(ScrollView):
    class Clicked(Message):
        def __init__(self, row: Row, x: int):
            super().__init__()
            self.row = row
            self.x = x

    can_focus = True

    def __init__(self, view_id: str):
        super().__init__(id=view_id)
        self.rows: list[Row] = []
        self.strips: dict[int, Strip] = {}
        self.built_width = 0

    @property
    def content_width(self) -> int:
        return max(1, self.scrollable_content_region.width)

    def set_rows(self, rows: list[Row]) -> None:
        self.rows = rows
        self.strips.clear()
        self.built_width = self.content_width
        self.virtual_size = Size(self.built_width, len(rows))
        self.refresh()

    def rebuild(self) -> None:
        raise NotImplementedError

    def on_resize(self, event: events.Resize) -> None:
        if self.content_width != self.built_width:
            self.rebuild()

    def render_line(self, y: int) -> Strip:
        index = self.scroll_offset.y + y
        width = self.size.width
        if not 0 <= index < len(self.rows):
            return Strip.blank(width, self.rich_style)
        row = self.rows[index]
        strip = self.strips.get(index)
        if strip is None:
            strip = Strip(row.text.render(self.app.console))
            self.strips[index] = strip
        return strip.crop_extend(0, width, row.fill)

    def on_click(self, event: events.Click) -> None:
        index = self.scroll_offset.y + event.y
        if 0 <= index < len(self.rows):
            event.stop()
            self.post_message(self.Clicked(self.rows[index], event.x))


class FileList(RowView):
    def __init__(self):
        super().__init__("diff-files")
        self.files: list[FileDiff] = []
        self.generated = 0
        self.show_generated = False

    def show(self, files: list[FileDiff], generated: int, show_generated: bool) -> None:
        self.files = files
        self.generated = generated
        self.show_generated = show_generated
        self.rebuild()

    def rebuild(self) -> None:
        width = self.content_width
        rows = [
            list_row(file, width) for file in self.files if not is_generated(file.path)
        ]
        if self.generated:
            rows.append(group_row(self.generated, self.show_generated))
            if self.show_generated:
                rows.extend(
                    list_row(file, width)
                    for file in self.files
                    if is_generated(file.path)
                )
        self.set_rows(rows)


class DiffCanvas(RowView):
    def __init__(self):
        super().__init__("diff-canvas")
        self.renders: list[FileRender] = []
        self.message = LOADING
        self.offsets: dict[str, int] = {}
        self.opened_new: set[str] = set()
        self.closed_tracked: set[str] = set()

    def show(self, renders: list[FileRender], message: str = "") -> None:
        anchor = self.anchor()
        self.renders = renders
        self.message = message
        self.rebuild()
        self.restore(anchor)

    def anchor(self) -> tuple[str, int] | None:
        index = self.scroll_offset.y
        if 0 <= index < len(self.rows) and self.rows[index].path in self.offsets:
            path = self.rows[index].path
            return path, index - self.offsets[path]
        return None

    def restore(self, anchor: tuple[str, int] | None) -> None:
        if anchor is not None and anchor[0] in self.offsets:
            self.scroll_to(
                y=self.offsets[anchor[0]] + anchor[1], animate=False, immediate=True
            )

    def is_collapsed(self, file: FileDiff) -> bool:
        if file.status == "?":
            return file.path not in self.opened_new
        return file.path in self.closed_tracked

    def toggle(self, path: str) -> None:
        render = next((item for item in self.renders if item.file.path == path), None)
        if render is None:
            return
        target = self.opened_new if render.file.status == "?" else self.closed_tracked
        target.symmetric_difference_update({path})
        anchor = self.anchor()
        self.rebuild()
        self.restore(anchor)

    def rebuild(self) -> None:
        width = self.content_width
        rows: list[Row] = []
        offsets: dict[str, int] = {}
        if not self.renders:
            rows.append(message_row(self.message))
        for render in self.renders:
            file = render.file
            if rows:
                rows.append(Row(Text(""), Style()))
            rows.append(rule_row(width))
            offsets[file.path] = len(rows)
            rows.append(header_row(file, width))
            rows.append(rule_row(width))
            rows.extend(self.body_rows(render, width))
        self.offsets = offsets
        self.set_rows(rows)

    def body_rows(self, render: FileRender, width: int) -> list[Row]:
        file = render.file
        path = file.path
        if self.is_collapsed(file):
            lines = file.additions
            return [
                message_row("New file, not staged yet.", path, "file"),
                message_row(f"Click to show its {lines} lines.", path, "file"),
            ]
        rows = [message_row(note, path) for note in self.notes(file)]
        for line in render.lines:
            rows.extend(
                wrapped_rows(line, render.gutter, width, self.app.console, path)
            )
        if file.hidden_lines:
            rows.append(
                message_row(f"… {file.hidden_lines} more lines not shown", path)
            )
        return rows

    @staticmethod
    def notes(file: FileDiff) -> list[str]:
        notes = []
        if file.binary:
            notes.append("Binary file, no preview")
        if file.note:
            notes.append(file.note)
        if not file.hunks and not file.binary and not file.note:
            if file.status == "R":
                notes.append("Renamed without content changes")
            elif file.status in ("A", "?"):
                notes.append("Empty file")
            else:
                notes.append("No content changes")
        return notes

    def show_file(self, path: str) -> bool:
        if path not in self.offsets:
            return False
        self.scroll_to(y=max(0, self.offsets[path] - 1), animate=False, immediate=True)
        return True


class DiffView(Vertical):
    BINDINGS = [("escape", "close", "Close")]

    def __init__(self, project: Path):
        super().__init__(id="diff-view")
        self.project = project
        self.source = "current"
        self.changes: Changes | None = None
        self.signature = ""
        self.show_generated = False
        self.generation = 0
        self.restore_focus: Widget | None = None
        self.display = False

    @property
    def canvas(self) -> DiffCanvas:
        return self.query_one(DiffCanvas)

    @property
    def files(self) -> FileList:
        return self.query_one(FileList)

    def compose(self) -> ComposeResult:
        with Horizontal(id="diff-top"):
            yield Static("", id="diff-top-spacer")
            yield Button("×", id="diff-close")
        with Horizontal(id="diff-summary"):
            yield Static(LOADING, id="diff-title")
            yield Button(self.source_label(), id="diff-source")
        yield FileList()
        yield DiffCanvas()

    def source_label(self) -> str:
        return f"source: {SOURCES[self.source]} ▾"

    async def open(self, changes: Changes | None, focus: bool = True) -> None:
        self.display = True
        if focus:
            self.restore_focus = self.app.focused
            self.call_after_refresh(self.canvas.focus)
        if changes is None:
            self.canvas.show([], LOADING)
        else:
            await self.show_changes(changes)

    def hide(self) -> None:
        self.display = False
        target, self.restore_focus = self.restore_focus, None
        if target is not None and target.is_attached:
            target.focus()

    def action_close(self) -> None:
        self.app.hide_diff()

    def visible_files(self) -> list[FileDiff]:
        files = self.changes.files if self.changes else []
        return [
            file for file in files if self.show_generated or not is_generated(file.path)
        ]

    async def show_changes(self, changes: Changes) -> None:
        self.changes = changes
        self.signature = changes.signature
        await self.apply()

    async def apply(self) -> None:
        self.generation += 1
        generation = self.generation
        changes = self.changes
        visible = self.visible_files()
        renders = await asyncio.to_thread(render_files, visible)
        if generation != self.generation:
            return
        if changes is None:
            message = LOADING
        elif not changes.available:
            message = NOT_A_REPOSITORY
        else:
            message = CLEAN if self.source == "current" else EMPTY_SOURCE
        everything = changes.files if changes else []
        generated = sum(is_generated(file.path) for file in everything)
        self.canvas.show(renders, message)
        self.files.show(visible, generated, self.show_generated)
        self.query_one("#diff-title", Static).update(heading_markup(changes))
        self.fit_file_list()

    def fit_file_list(self) -> None:
        limit = max(3, self.size.height // FILES_HEIGHT_SHARE)
        self.files.styles.height = max(1, min(len(self.files.rows), limit)) + 1

    def on_resize(self, event: events.Resize) -> None:
        self.fit_file_list()

    async def set_source(self, source: str) -> None:
        if source not in SOURCES or source == self.source:
            return
        self.source = source
        self.signature = ""
        self.query_one("#diff-source", Button).label = self.source_label()
        if source == "current" and self.app.changes is not None:
            await self.show_changes(self.app.changes)
        else:
            await self.poll(force=True)

    async def poll(self, force: bool = False) -> None:
        source = self.source
        changes = await collect(self.project, "" if force else self.signature, source)
        if source != self.source or changes.unchanged or changes.failed:
            return
        await self.show_changes(changes)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        name = event.button.id
        if name == "diff-close":
            self.app.hide_diff()
        elif name == "diff-source":
            self.app.choose_diff_source(event.button.region)
        else:
            return
        event.stop()

    async def on_row_view_clicked(self, event: RowView.Clicked) -> None:
        event.stop()
        row = event.row
        if row.action == "jump":
            self.canvas.show_file(row.path)
        elif row.action == "group":
            self.show_generated = not self.show_generated
            await self.apply()
        elif row.action == "file":
            if 0 <= row.ask_from <= event.x:
                self.app.ask_about(row.path)
            else:
                self.canvas.toggle(row.path)

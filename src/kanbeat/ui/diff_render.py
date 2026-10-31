from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import PurePosixPath
from typing import Any

from pygments.lexer import Lexer
from pygments.lexers import get_lexer_for_filename
from pygments.token import Token
from pygments.util import ClassNotFound
from rich.console import Console
from rich.style import Style
from rich.text import Text

from ..diff import DiffLine, FileDiff

TEXT_COLOR = "#e6edf7"
NAME_COLOR = "#f2f7fd"
MUTED_COLOR = "#8d9aab"
GUTTER_COLOR = "#7d8a9b"
RULE_COLOR = "#5a6575"
ADD_COLOR = "#5ee08a"
REMOVE_COLOR = "#ff5d6c"
BACKGROUNDS = {"+": "#0d3a1b", "-": "#4a1620", " ": ""}
MARK_BACKGROUNDS = {"+": "#1d6b36", "-": "#862a3b"}
SIGN_COLORS = {"+": ADD_COLOR, "-": REMOVE_COLOR, " ": GUTTER_COLOR}
TOKEN_STYLES = {
    Token.Comment: Style(color="#9aa7bb", italic=True),
    Token.Keyword: Style(color="#c4a7ff"),
    Token.Keyword.Constant: Style(color="#bd93f9"),
    Token.Operator.Word: Style(color="#c4a7ff"),
    Token.String: Style(color="#f1e07a"),
    Token.Number: Style(color="#bd93f9"),
    Token.Literal: Style(color="#bd93f9"),
    Token.Name.Function: Style(color="#8ab4ff"),
    Token.Name.Class: Style(color="#ffd479"),
    Token.Name.Decorator: Style(color="#ffd479"),
    Token.Name.Builtin: Style(color="#7fdcff"),
    Token.Name.Tag: Style(color="#8ab4ff"),
    Token.Name.Attribute: Style(color="#ffd479"),
    Token.Generic.Heading: Style(color="#8ab4ff", bold=True),
}
PLAIN = Style(color=TEXT_COLOR)
HIGHLIGHT_LINE_LIMIT = 400
ASK_LABEL = "[ ask ]"
MIN_WRAP = 10


@dataclass
class RenderedLine:
    kind: str
    number: int | None
    content: Text


@dataclass
class FileRender:
    file: FileDiff
    gutter: int
    lines: list[RenderedLine]


@dataclass
class Row:
    text: Text
    fill: Style
    action: str = ""
    path: str = ""
    ask_from: int = -1


@lru_cache(maxsize=512)
def token_style(token: Any) -> Style:
    current = token
    while current is not None:
        style = TOKEN_STYLES.get(current)
        if style is not None:
            return style
        current = current.parent
    return PLAIN


@lru_cache(maxsize=256)
def lexer_for(name: str) -> Lexer | None:
    try:
        return get_lexer_for_filename(name, stripnl=False, ensurenl=False)
    except ClassNotFound:
        return None


def lexer_for_path(path: str) -> Lexer | None:
    pure = PurePosixPath(path)
    return lexer_for(pure.suffix.lower() or pure.name)


def highlighted(text: str, lexer: Lexer | None) -> list[tuple[str, Style]]:
    if lexer is None or not text or len(text) > HIGHLIGHT_LINE_LIMIT:
        return [(text, PLAIN)]
    try:
        parts = [(value, token_style(token)) for token, value in lexer.get_tokens(text)]
    except Exception:
        return [(text, PLAIN)]
    if "".join(value for value, _ in parts) != text:
        return [(text, PLAIN)]
    return parts


def line_content(line: DiffLine, lexer: Lexer | None) -> Text:
    text = Text(no_wrap=False, overflow="fold")
    for value, style in highlighted(line.text, lexer):
        text.append(value, style)
    for begin, end in line.marks:
        text.stylize(Style(bgcolor=MARK_BACKGROUNDS[line.kind]), begin, end)
    return text


def gutter_width(file: FileDiff) -> int:
    highest = 0
    for hunk in file.hunks:
        for line in hunk.lines:
            highest = max(highest, line.old_number or 0, line.new_number or 0)
    return max(3, len(str(highest)))


def render_file(file: FileDiff) -> FileRender:
    lexer = lexer_for_path(file.path)
    lines = [
        RenderedLine(
            line.kind,
            line.old_number if line.kind == "-" else line.new_number,
            line_content(line, lexer),
        )
        for hunk in file.hunks
        for line in hunk.lines
    ]
    return FileRender(file=file, gutter=gutter_width(file), lines=lines)


def render_files(files: list[FileDiff]) -> list[FileRender]:
    return [render_file(file) for file in files]


def line_fill(kind: str) -> Style:
    background = BACKGROUNDS[kind]
    return Style(bgcolor=background) if background else Style()


def wrapped_rows(
    line: RenderedLine, gutter: int, width: int, console: Console, path: str
) -> list[Row]:
    room = max(MIN_WRAP, width - gutter - 2)
    pieces = list(line.content.wrap(console, room, overflow="fold")) or [Text("")]
    fill = line_fill(line.kind)
    rows = []
    for index, piece in enumerate(pieces):
        text = Text(no_wrap=True, overflow="crop", style=fill)
        if index == 0:
            number = "" if line.number is None else str(line.number)
            text.append(f"{number:>{gutter}} ", Style(color=GUTTER_COLOR))
            text.append(line.kind, Style(color=SIGN_COLORS[line.kind], bold=True))
        else:
            text.append(" " * (gutter + 2))
        text.append_text(piece)
        rows.append(Row(text, fill, path=path))
    return rows


def rule_row(width: int) -> Row:
    return Row(Text("─" * width, style=Style(color=RULE_COLOR), no_wrap=True), Style())


def header_row(file: FileDiff, width: int) -> Row:
    suffix = " (untracked)" if file.status == "?" else ""
    name = file.path
    if file.old_path:
        name = f"{file.old_path} → {file.path}"
    room = max(4, width - len(ASK_LABEL) - len(suffix) - 2)
    label = Text(name, style=Style(color=NAME_COLOR, bold=True), no_wrap=True)
    label.truncate(room, overflow="ellipsis")
    text = Text(no_wrap=True, overflow="crop")
    text.append_text(label)
    text.append(suffix, Style(color=MUTED_COLOR))
    padding = max(1, width - text.cell_len - len(ASK_LABEL))
    text.append(" " * padding)
    ask_from = text.cell_len
    text.append(ASK_LABEL, Style(color=NAME_COLOR, bold=True))
    return Row(text, Style(), "file", file.path, ask_from)


def message_row(message: str, path: str = "", action: str = "") -> Row:
    return Row(
        Text(message, style=Style(color=MUTED_COLOR), no_wrap=True, overflow="fold"),
        Style(),
        action,
        path,
    )


def list_row(file: FileDiff, width: int) -> Row:
    counts = Text(no_wrap=True)
    if file.additions:
        counts.append(f"+{file.additions}", Style(color=ADD_COLOR))
    if file.deletions:
        counts.append(" " if file.additions else "")
        counts.append(f"-{file.deletions}", Style(color=REMOVE_COLOR))
    room = max(4, width - counts.cell_len - 2)
    label = Text(file.path, style=Style(color=MUTED_COLOR), no_wrap=True)
    label.truncate(room, overflow="ellipsis")
    text = Text(no_wrap=True, overflow="crop")
    text.append_text(label)
    text.append(" " * max(1, width - label.cell_len - counts.cell_len))
    text.append_text(counts)
    return Row(text, Style(), "jump", file.path)


def group_row(count: int, shown: bool) -> Row:
    state = "hide" if shown else "show"
    return message_row(f"{count} tests/generated ({state})", action="group")

from __future__ import annotations

import io
from pathlib import Path
import subprocess

from PIL import Image, ImageDraw, ImageFont
from rich.cells import cell_len
from rich.console import Console
from rich.segment import Segment

DEFAULT_BACKGROUND = (11, 16, 25)
DEFAULT_FOREGROUND = (217, 228, 243)
CAPTION_BACKGROUND = (8, 12, 19)
CAPTION_TEXT = (226, 234, 245)
TAG_BACKGROUND = (126, 229, 213)
TAG_TEXT = (8, 12, 19)
PATH_PLACEHOLDER = "~/demo-project"

BLOCKS = {
    "▀": (0, 0, 1, 0.5),
    "▁": (0, 7 / 8, 1, 1),
    "▂": (0, 6 / 8, 1, 1),
    "▃": (0, 5 / 8, 1, 1),
    "▄": (0, 0.5, 1, 1),
    "▅": (0, 3 / 8, 1, 1),
    "▆": (0, 2 / 8, 1, 1),
    "▇": (0, 1 / 8, 1, 1),
    "█": (0, 0, 1, 1),
    "▉": (0, 0, 7 / 8, 1),
    "▊": (0, 0, 6 / 8, 1),
    "▋": (0, 0, 5 / 8, 1),
    "▌": (0, 0, 0.5, 1),
    "▍": (0, 0, 3 / 8, 1),
    "▎": (0, 0, 2 / 8, 1),
    "▏": (0, 0, 1 / 8, 1),
    "▐": (0.5, 0, 1, 1),
    "▔": (0, 0, 1, 1 / 8),
    "▕": (7 / 8, 0, 1, 1),
}


def font_file(pattern: str) -> str:
    result = subprocess.run(
        ["fc-match", "-f", "%{file}", pattern],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def blend(top: tuple[int, int, int], bottom: tuple[int, int, int], amount: float):
    return tuple(round(t * amount + b * (1 - amount)) for t, b in zip(top, bottom))


class FrameRenderer:
    def __init__(
        self,
        mono: str,
        mono_bold: str,
        sans_bold: str,
        cell_width: int = 8,
        cell_height: int = 17,
    ):
        self.cell_width = cell_width
        self.cell_height = cell_height
        size = cell_width / 0.6
        self.regular = ImageFont.truetype(mono, size)
        self.bold = ImageFont.truetype(mono_bold, size)
        self.caption_font = ImageFont.truetype(sans_bold, 15)
        ascent, descent = self.regular.getmetrics()
        self.baseline = (cell_height - (ascent + descent)) // 2 + ascent
        self.caption_height = 36

    @staticmethod
    def segments(app, hidden_text: dict[str, str]) -> list[list[Segment]]:
        width, height = app.size
        console = Console(
            width=width,
            height=height,
            file=io.StringIO(),
            force_terminal=True,
            color_system="truecolor",
            legacy_windows=False,
            safe_box=False,
        )
        update = app.screen._compositor.render_update(
            full=True, screen_stack=app._background_screens
        )
        flat = Segment.filter_control(console.render(update))
        lines = list(Segment.split_and_crop_lines(flat, width, pad=True))
        for index, line in enumerate(lines):
            lines[index] = FrameRenderer.rewrite(line, width, hidden_text)
        return lines

    @staticmethod
    def rewrite(line: list[Segment], width: int, replacements: dict[str, str]):
        if not any(old in segment.text for segment in line for old in replacements):
            return line
        rewritten = []
        for segment in line:
            text = segment.text
            for old, new in replacements.items():
                text = text.replace(old, new)
            rewritten.append(Segment(text, segment.style))
        used = sum(cell_len(segment.text) for segment in rewritten)
        if used < width:
            rewritten.append(Segment(" " * (width - used), rewritten[-1].style))
        return rewritten

    @staticmethod
    def colors(style):
        foreground = DEFAULT_FOREGROUND
        background = DEFAULT_BACKGROUND
        if style is not None:
            if style.color is not None and not style.color.is_default:
                foreground = tuple(style.color.get_truecolor())
            if style.bgcolor is not None and not style.bgcolor.is_default:
                background = tuple(style.bgcolor.get_truecolor())
            if style.reverse:
                foreground, background = background, foreground
            if style.dim:
                foreground = blend(foreground, background, 0.6)
        return foreground, background

    def ui_image(
        self, app, hidden_text: dict[str, str], forbidden: tuple[str, ...] = ()
    ) -> Image.Image:
        lines = self.segments(app, hidden_text)
        for line in lines:
            text = "".join(segment.text for segment in line)
            for item in forbidden:
                if item and item in text:
                    raise ValueError(f"Frame would expose {item!r}: {text.strip()!r}")
        width, height = app.size
        image = Image.new(
            "RGB",
            (width * self.cell_width, height * self.cell_height),
            DEFAULT_BACKGROUND,
        )
        draw = ImageDraw.Draw(image)
        for row, line in enumerate(lines):
            column = 0
            for segment in line:
                style = segment.style
                foreground, background = self.colors(style)
                columns = cell_len(segment.text)
                if not columns:
                    continue
                top = row * self.cell_height
                draw.rectangle(
                    (
                        column * self.cell_width,
                        top,
                        (column + columns) * self.cell_width - 1,
                        top + self.cell_height - 1,
                    ),
                    fill=background,
                )
                font = self.bold if style is not None and style.bold else self.regular
                offset = column
                for character in segment.text:
                    span = cell_len(character)
                    left = offset * self.cell_width
                    if character in BLOCKS:
                        x0, y0, x1, y1 = BLOCKS[character]
                        draw.rectangle(
                            (
                                left + round(x0 * self.cell_width),
                                top + round(y0 * self.cell_height),
                                left + round(x1 * self.cell_width) - 1,
                                top + round(y1 * self.cell_height) - 1,
                            ),
                            fill=foreground,
                        )
                    elif character.strip() and span:
                        draw.text(
                            (left, top + self.baseline),
                            character,
                            font=font,
                            fill=foreground,
                            anchor="ls",
                        )
                    if style is not None and style.underline and span:
                        y = top + self.baseline + 2
                        draw.line(
                            (left, y, left + span * self.cell_width - 1, y),
                            fill=foreground,
                        )
                    offset += span
                column += columns
        return image

    def compose(self, ui: Image.Image, tag: str, caption: str) -> Image.Image:
        frame = Image.new(
            "RGB", (ui.width, ui.height + self.caption_height), CAPTION_BACKGROUND
        )
        frame.paste(ui, (0, 0))
        draw = ImageDraw.Draw(frame)
        middle = ui.height + self.caption_height // 2
        tag_width = int(draw.textlength(tag, font=self.caption_font)) + 18
        draw.rounded_rectangle(
            (10, middle - 12, 10 + tag_width, middle + 12),
            radius=6,
            fill=TAG_BACKGROUND,
        )
        draw.text((19, middle), tag, font=self.caption_font, fill=TAG_TEXT, anchor="lm")
        draw.text(
            (10 + tag_width + 14, middle),
            caption,
            font=self.caption_font,
            fill=CAPTION_TEXT,
            anchor="lm",
        )
        return frame


def save_gif(frames: list[tuple[Image.Image, int]], path: Path) -> None:
    sample = Image.new(
        "RGB",
        (
            frames[0][0].width,
            sum(image.height for image, _ in frames[:: max(1, len(frames) // 8)]),
        ),
    )
    offset = 0
    for image, _ in frames[:: max(1, len(frames) // 8)]:
        sample.paste(image, (0, offset))
        offset += image.height
    palette = sample.quantize(
        colors=255, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE
    )
    converted = [
        image.quantize(palette=palette, dither=Image.Dither.NONE) for image, _ in frames
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    converted[0].save(
        path,
        save_all=True,
        append_images=converted[1:],
        duration=[duration for _, duration in frames],
        loop=0,
        optimize=False,
        disposal=1,
    )

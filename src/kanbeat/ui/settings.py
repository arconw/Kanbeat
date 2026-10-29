from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Button, Select, Static, TextArea

from .options import HARNESS


def harness_value(value, default="codex"):
    return (
        value
        if isinstance(value, str) and value in {key for _, key in HARNESS}
        else default
    )


def harness_warning(value, default="codex"):
    if default == "" and value in (None, ""):
        return ""
    if isinstance(value, str) and value in {key for _, key in HARNESS}:
        return ""
    shown = "Project default" if default == "" else default.title()
    return f"Saved harness {repr(value)[:80]} is unavailable. Showing {shown}; Save to replace it."


class Settings(VerticalScroll):
    def __init__(self, snapshot: dict[str, Any]):
        super().__init__(id="settings")
        self.config = snapshot.get("config") or {}

    def compose(self) -> ComposeResult:
        yield Static("SETTINGS / PROJECT DEFAULTS", classes="page-heading")
        yield Static("Default harness", classes="field")
        yield Select(
            HARNESS,
            value=harness_value(self.config.get("default_harness")),
            id="cfg-harness",
        )
        warning = harness_warning(self.config.get("default_harness"))
        notice = Static(warning, id="cfg-harness-warning", classes="hint", markup=False)
        notice.display = bool(warning)
        yield notice
        for field, title in (
            ("global_cp", "Global CP"),
            ("developer_prompt", "Developer Prompt"),
            ("reviewer_prompt", "Reviewer / QA Prompt"),
        ):
            yield Static(title, classes="field")
            yield TextArea(
                str(self.config.get(field) or ""),
                id="cfg-" + field,
                classes="settings-editor",
            )
        yield Static("TRANSITION HOOK", classes="section-title")
        hook = self.config.get("transition_hook") or {}
        yield Select(
            [("Project default", "")] + HARNESS,
            value=harness_value(hook.get("harness"), ""),
            allow_blank=False,
            id="cfg-hook-harness",
        )
        warning = harness_warning(hook.get("harness"), "")
        notice = Static(
            warning, id="cfg-hook-harness-warning", classes="hint", markup=False
        )
        notice.display = bool(warning)
        yield notice
        yield TextArea(
            str(hook.get("prompt") or ""),
            id="cfg-hook-prompt",
            classes="settings-editor",
        )
        yield Static(
            "Empty prompt disables hooks. Hooks run after moves on Play. "
            "Failures never revert tickets; interrupted hooks are not replayed.",
            classes="hint",
        )
        yield Button("Save settings", id="settings-save", variant="primary")

    def update_snapshot(self, snapshot) -> None:
        config = snapshot.get("config") or {}
        fields = [("default_harness", "#cfg-harness", Select)] + [
            (field, "#cfg-" + field, TextArea)
            for field in ("global_cp", "developer_prompt", "reviewer_prompt")
        ]
        for field, selector, kind in fields:
            control = self.query_one(selector, kind)
            old = (
                harness_value(self.config.get(field))
                if kind is Select
                else str(self.config.get(field) or "")
            )
            new = (
                harness_value(config.get(field))
                if kind is Select
                else str(config.get(field) or "")
            )
            current = control.value if kind is Select else control.text
            if current == old and new != old:
                if kind is Select:
                    control.value = new
                else:
                    control.load_text(new)
        old_hook = self.config.get("transition_hook") or {}
        new_hook = config.get("transition_hook") or {}
        for field, selector, kind in (
            ("harness", "#cfg-hook-harness", Select),
            ("prompt", "#cfg-hook-prompt", TextArea),
        ):
            control = self.query_one(selector, kind)
            current = control.value if kind is Select else control.text
            old = (
                harness_value(old_hook.get(field), "")
                if kind is Select
                else str(old_hook.get(field) or "")
            )
            new = (
                harness_value(new_hook.get(field), "")
                if kind is Select
                else str(new_hook.get(field) or "")
            )
            if current == old and old != new:
                if kind is Select:
                    control.value = new
                else:
                    control.load_text(new)
        self.config = config
        for selector, value, default in (
            ("#cfg-harness-warning", config.get("default_harness"), "codex"),
            ("#cfg-hook-harness-warning", new_hook.get("harness"), ""),
        ):
            notice = self.query_one(selector, Static)
            warning = harness_warning(value, default)
            notice.update(warning)
            notice.display = bool(warning)

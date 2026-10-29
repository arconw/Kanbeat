from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Button, Select, Static, TextArea

from .options import HARNESS


class Settings(VerticalScroll):
    def __init__(self, snapshot: dict[str, Any]):
        super().__init__(id="settings")
        self.config = snapshot.get("config") or {}

    def compose(self) -> ComposeResult:
        yield Static("SETTINGS / PROJECT DEFAULTS", classes="page-heading")
        yield Static("Default harness", classes="field")
        yield Select(
            HARNESS,
            value=self.config.get("default_harness") or "codex",
            id="cfg-harness",
        )
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
            value=hook.get("harness") or "",
            allow_blank=False,
            id="cfg-hook-harness",
        )
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
            old = self.config.get(field) or ""
            new = config.get(field) or ""
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
            if current == (old_hook.get(field) or "") and old_hook.get(
                field
            ) != new_hook.get(field):
                if kind is Select:
                    control.value = new_hook.get(field) or ""
                else:
                    control.load_text(new_hook.get(field) or "")
        self.config = config

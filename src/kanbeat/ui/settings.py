from __future__ import annotations

import asyncio
from typing import Any

from textual.app import ComposeResult
from textual.css.query import NoMatches
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Collapsible, Select, Static, TextArea

from .options import HARNESS


HISTORY_FIRST = 5
HISTORY_STEP = 20
HISTORY_MAX = 100


def history_title(entry: dict[str, Any]) -> str:
    keys = ", ".join(change["key"] for change in entry["changes"])
    return f"#{entry['id']}  {entry['time'].replace('T', ' ')[:16]}  {entry['source']}  {keys}"


def history_details(entry: dict[str, Any]) -> str:
    blocks = []
    for change in entry["changes"]:
        blocks.append(
            f"{change['key']}\n  was: {change['old']!s}\n  now: {change['new']!s}"
        )
    return "\n\n".join(blocks)


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
        self.history_limit = HISTORY_FIRST
        self.history_seen: int | None = None
        self.history_open: set[int] = set()
        self.history_lock = asyncio.Lock()
        self.history_error = ""

    def compose(self) -> ComposeResult:
        yield Static("SETTINGS / PROJECT DEFAULTS", classes="page-heading")
        yield Static("Default harness", classes="field")
        with Horizontal(classes="field-row"):
            yield Select(
                HARNESS,
                value=harness_value(self.config.get("default_harness")),
                id="cfg-harness",
            )
            yield Button("Open", id="cfg-open-harness")
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
        with Horizontal(classes="field-row"):
            yield Select(
                [("Project default", "")] + HARNESS,
                value=harness_value(hook.get("harness"), ""),
                allow_blank=False,
                id="cfg-hook-harness",
            )
            yield Button("Open", id="cfg-open-hook-harness")
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
        yield Static("HISTORY", classes="section-title")
        yield Vertical(id="cfg-history")
        yield Button("Show more", id="cfg-history-more")

    async def on_mount(self) -> None:
        self.query_one("#cfg-history-more", Button).display = False
        await self.load_history()

    async def load_history(self) -> None:
        async with self.history_lock:
            try:
                result = await self.app.rpc(
                    {"op": "config_history", "limit": self.history_limit}
                )
                entries = result["history"]
                total = result["total"]
            except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
                if str(exc) != self.history_error:
                    self.history_error = str(exc)
                    self.app.notify(
                        f"Could not load the history: {exc}", severity="warning"
                    )
                return
            self.history_error = ""
            self.history_seen = self.app.snapshot.get("config_seq", 0)
            try:
                container = self.query_one("#cfg-history", Vertical)
                more = self.query_one("#cfg-history-more", Button)
            except NoMatches:
                return
            await container.remove_children()
            if not entries:
                await container.mount(
                    Static("No settings changes yet.", classes="hint", markup=False)
                )
            await container.mount(
                *(
                    Collapsible(
                        Static(
                            history_details(entry),
                            classes="history-detail",
                            markup=False,
                        ),
                        Button("Revert", id=f"cfg-revert-{entry['id']}"),
                        title=history_title(entry),
                        collapsed=entry["id"] not in self.history_open,
                        id=f"cfg-entry-{entry['id']}",
                    )
                    for entry in entries
                )
            )
            more.display = len(entries) < total and self.history_limit < HISTORY_MAX
            more.label = f"Show more ({len(entries)} of {total})"

    def on_collapsible_expanded(self, event: Collapsible.Expanded) -> None:
        self.track_entry(event.collapsible, True)

    def on_collapsible_collapsed(self, event: Collapsible.Collapsed) -> None:
        self.track_entry(event.collapsible, False)

    def track_entry(self, collapsible: Collapsible, opened: bool) -> None:
        name = collapsible.id or ""
        if name.startswith("cfg-entry-"):
            number = int(name[len("cfg-entry-") :])
            (self.history_open.add if opened else self.history_open.discard)(number)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "cfg-history-more":
            return
        event.stop()
        self.history_limit = min(HISTORY_MAX, self.history_limit + HISTORY_STEP)
        await self.load_history()

    def update_snapshot(self, snapshot) -> None:
        config = snapshot.get("config") or {}
        if snapshot.get("config_seq", 0) != self.history_seen:
            self.run_worker(self.load_history, exclusive=True, group="history")
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

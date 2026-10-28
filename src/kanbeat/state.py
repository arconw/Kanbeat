"""Private recoverable runtime. Tickets and columns never depend on .state."""
from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from .filesystem import atomic_json, Board

DEFAULT_CONFIG = {
    "default_harness": "codex",
    "global_cp": "",
    "developer_prompt": "Implement the task. You may write tests but do not run them.",
    "reviewer_prompt": "Review and test the implementation. Prefer headless tests if possible.",
    "transition_hook": {"harness": "", "prompt": ""},
}


class LocalState:
    def __init__(self, board: Board):
        self.board = board
        self.path = board.root / ".state" / "supervisor.json"
        self.config_path = board.root / "config.json"
        self.data = self._read()
        self.config = self._config()

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"execution": "stopped", "tickets": {}, "custom": {}, "notices": []}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("State must be JSON object")
            value.setdefault("execution", "stopped")
            value.setdefault("tickets", {})
            value.setdefault("custom", {})
            value.setdefault("notices", [])
            return value
        except (ValueError, OSError) as exc:
            raise RuntimeError(f"Invalid supervisor state: {exc}") from exc

    def _config(self) -> dict[str, Any]:
        if not self.config_path.exists():
            return deepcopy(DEFAULT_CONFIG)
        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("config.json must be an object")
        config = {**deepcopy(DEFAULT_CONFIG), **data}
        config["transition_hook"] = self.validate_hook(config["transition_hook"])
        return config

    def save(self) -> None:
        atomic_json(self.path, self.data)

    @staticmethod
    def validate_hook(hook: Any) -> dict[str, str]:
        if not isinstance(hook, dict):
            raise ValueError("Transition hook must be an object")
        if hook.get("harness", "") not in ("", "codex", "claude", "opencode", "pi"):
            raise ValueError("Invalid transition hook harness")
        if not isinstance(hook.get("prompt", ""), str):
            raise ValueError("Transition hook prompt must be text")
        return {"harness": hook.get("harness", ""), "prompt": hook.get("prompt", "")}

    def save_config(self, values: dict[str, Any]) -> None:
        if "transition_hook" in values:
            values = {**values, "transition_hook": self.validate_hook(values["transition_hook"])}
        editable = set(DEFAULT_CONFIG)
        for key in editable:
            if key in values:
                self.config[key] = values[key]
        atomic_json(self.config_path, self.config)

    def ticket(self, ticket_id: str) -> dict[str, Any]:
        return self.data["tickets"].setdefault(ticket_id, {
            "developer": {}, "reviewers": [], "runtime": {}, "phase": "",
            "review_results": {},
        })

    def notice(self, text: str, level: str = "information") -> None:
        notices = self.data.setdefault("notices", [])
        serial = int(self.data.get("notice_seq", 0)) + 1
        self.data["notice_seq"] = serial
        notices.append({"id": serial, "message": text, "level": level})
        del notices[:-30]

    def clean_deleted_tickets(self, current_ids: set[str]) -> None:
        # Only prune data for nonexistent tickets. Archive is still a ticket.
        for ticket_id in list(self.data["tickets"]):
            if ticket_id not in current_ids:
                del self.data["tickets"][ticket_id]

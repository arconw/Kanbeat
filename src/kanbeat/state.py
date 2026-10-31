"""Private recoverable runtime. Tickets and columns never depend on .state."""

from __future__ import annotations

import json
from contextlib import suppress
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from .config_keys import HARNESSES, coerce, flatten, read_key, write_key
from .filesystem import atomic_json, Board

HISTORY_LIMIT = 100
HISTORY_PREVIEW = 600

DEFAULT_CONFIG = {
    "default_harness": "codex",
    "max_parallel": 3,
    "global_cp": "",
    "developer_prompt": "Implement the task. You may write tests but do not run them.",
    "reviewer_prompt": "Review and test the implementation. Prefer headless tests if possible.",
    "transition_hook": {"harness": "", "prompt": ""},
}


def preview(value: Any) -> Any:
    if isinstance(value, str) and len(value) > HISTORY_PREVIEW:
        return value[:HISTORY_PREVIEW] + "…"
    return value


class LocalState:
    def __init__(self, board: Board):
        self.board = board
        self.path = board.root / ".state" / "supervisor.json"
        self.config_path = board.root / "config.json"
        self.history_path = board.root / ".state" / "config-history.json"
        self.data = self._read()
        self.config = self._config()
        self.history = self._read_history()

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
        if hook.get("harness", "") not in ("", *HARNESSES):
            raise ValueError("Invalid transition hook harness")
        if not isinstance(hook.get("prompt", ""), str):
            raise ValueError("Transition hook prompt must be text")
        return {"harness": hook.get("harness", ""), "prompt": hook.get("prompt", "")}

    def save_config(self, values: dict[str, Any]) -> None:
        if "transition_hook" in values:
            values = {
                **values,
                "transition_hook": self.validate_hook(values["transition_hook"]),
            }
        self.change_config(flatten(values), "tui")

    def _read_history(self) -> list[dict[str, Any]]:
        try:
            value = json.loads(self.history_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        entries = value.get("entries") if isinstance(value, dict) else None
        if not isinstance(entries, list):
            return []
        return [
            entry
            for entry in entries
            if isinstance(entry, dict)
            and isinstance(entry.get("id"), int)
            and isinstance(entry.get("changes"), list)
        ][-HISTORY_LIMIT:]

    def config_history(self) -> list[dict[str, Any]]:
        return self.history

    def change_config(
        self, changes: dict[str, Any], source: str, trusted: bool = False
    ) -> dict[str, Any] | None:
        updated = deepcopy(self.config)
        rows = []
        for key, value in changes.items():
            value = value if trusted else coerce(key, value)
            old = read_key(self.config, key)
            if value != old:
                rows.append({"key": key, "old": old, "new": value})
                write_key(updated, key, value)
        if not rows:
            return None
        last = self.history[-1]["id"] if self.history else 0
        serial = max(int(self.data.get("config_seq", 0)), last) + 1
        entry = {
            "id": serial,
            "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": source,
            "changes": rows,
        }
        history = [*self.history, entry][-HISTORY_LIMIT:]
        atomic_json(self.history_path, {"entries": history})
        try:
            atomic_json(self.config_path, updated)
        except BaseException:
            with suppress(OSError):
                atomic_json(self.history_path, {"entries": self.history})
            raise
        self.history = history
        self.config = updated
        self.data["config_seq"] = serial
        self.save()
        return entry

    def revert_config(
        self, entry_id: int, source: str
    ) -> tuple[dict[str, Any] | None, list[str]]:
        entry = next(
            (row for row in self.config_history() if row["id"] == entry_id), None
        )
        if entry is None:
            raise ValueError(f"No config change #{entry_id} in the history")
        applicable = {}
        skipped = []
        for row in entry["changes"]:
            if read_key(self.config, row["key"]) == row["new"]:
                applicable[row["key"]] = row["old"]
            elif read_key(self.config, row["key"]) != row["old"]:
                skipped.append(row["key"])
        if not applicable and skipped:
            raise ValueError(
                f"Change #{entry_id} cannot be reverted: {', '.join(skipped)} "
                "changed again later. Revert the later change first"
            )
        restored = self.change_config(applicable, source, trusted=True)
        if restored is None:
            raise ValueError(f"Change #{entry_id} is already undone")
        return restored, skipped

    def history_view(self, limit: int) -> dict[str, Any]:
        history = self.config_history()
        rows = []
        for entry in reversed(history[-max(1, min(limit, HISTORY_LIMIT)) :]):
            rows.append(
                {
                    **entry,
                    "changes": [
                        {
                            "key": change["key"],
                            "old": preview(change["old"]),
                            "new": preview(change["new"]),
                        }
                        for change in entry["changes"]
                    ],
                }
            )
        return {"history": rows, "total": len(history)}

    def ticket(self, ticket_id: str) -> dict[str, Any]:
        return self.data["tickets"].setdefault(
            ticket_id,
            {
                "developer": {},
                "reviewers": [],
                "runtime": {},
                "phase": "",
                "review_results": {},
            },
        )

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

from __future__ import annotations

import json
from pathlib import Path

from ..filesystem import atomic_json


class ViewPreferences:
    def __init__(self, project: Path):
        self.path = project / ".kanbeat" / ".state" / "ui.json"
        self.collapsed = self.read()

    def read(self) -> dict[str, bool]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(value, dict):
            return {}
        return {key: state for key, state in value.items() if isinstance(state, bool)}

    def is_collapsed(self, section: str, default: bool = False) -> bool:
        return self.collapsed.get(section, default)

    def remember(self, section: str, collapsed: bool) -> None:
        if self.collapsed.get(section) == collapsed:
            return
        values = self.read()
        values[section] = collapsed
        atomic_json(self.path, values)
        self.collapsed = values

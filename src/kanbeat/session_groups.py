from __future__ import annotations

from typing import Any
import uuid

from .native import available, process_identity
from .state import LocalState
from .terminals import Terminals


def display_name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 64:
        raise ValueError("Name must contain 1..64 characters")
    if any(ord(char) < 32 for char in value):
        raise ValueError("Name cannot contain control characters")
    return value.strip()


class SessionGroups:
    def __init__(self, state: LocalState, terminals: Terminals):
        self.state = state
        self.terminals = terminals
        self.groups = state.data.setdefault("custom", {}).setdefault("groups", {})

    def group(self, group_id: str) -> str:
        if group_id and group_id not in self.groups:
            raise ValueError("Unknown session group")
        return group_id

    def custom(self, terminal_id: str) -> dict[str, Any]:
        entry = self.terminals.entries.get(terminal_id)
        if entry is None or entry.get("owner") != "custom":
            raise ValueError("Unknown independent session")
        return entry

    def inactive(self, entry: dict[str, Any]) -> None:
        if entry.get("status") == "recovery_blocked":
            pid = entry.get("pid")
            current = process_identity(int(pid)) if pid is not None else None
            saved = entry.get("process_identity")
            if current is None or (saved and current != saved):
                entry["status"] = "interrupted"
                entry.pop("pid", None)
                entry.pop("process_identity", None)
        if entry.get("status") in (
            "starting",
            "running",
            "stopping",
            "recovery_blocked",
        ):
            raise ValueError("Stop the session and resolve orphan recovery first")

    async def dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        op = request.get("op")
        if op in ("group_create", "group_rename"):
            name = display_name(request.get("name"))
            group_id = (
                uuid.uuid4().hex
                if op == "group_create"
                else self.group(str(request.get("group") or ""))
            )
            if not group_id:
                raise ValueError("The ungrouped section cannot be renamed")
            if any(
                row["name"].casefold() == name.casefold() and key != group_id
                for key, row in self.groups.items()
            ):
                raise ValueError("Group name already exists")
            self.groups[group_id] = {"name": name}
            result = {"group": group_id}
        elif op == "group_delete":
            group_id = self.group(str(request.get("group") or ""))
            if not group_id or request.get("confirmed") is not True:
                raise ValueError("Explicit group removal confirmation required")
            if any(
                e.get("owner") == "custom" and e.get("group") == group_id
                for e in self.terminals.entries.values()
            ):
                raise ValueError("Move or remove sessions before removing this group")
            del self.groups[group_id]
            result = {}
        elif op == "session_config_open":
            harness = str(
                request.get("harness") or self.state.config["default_harness"]
            )
            if not available(harness):
                raise ValueError("Independent session harness unavailable: " + harness)
            matches = [
                entry
                for entry in self.terminals.entries.values()
                if entry.get("owner") == "custom"
                and entry.get("role") == "config"
                and entry.get("harness") == harness
            ]
            entry = next(
                (
                    row
                    for row in reversed(matches)
                    if row.get("status") in ("starting", "running", "stopping")
                ),
                matches[-1] if matches else None,
            )
            if entry is not None:
                attached = entry["id"] in self.terminals.sessions
                status = entry.get("status")
                if attached and status == "stopping":
                    raise ValueError(
                        "Terminal is still stopping; wait for it to exit before reopening"
                    )
                if attached and status in ("starting", "running"):
                    return {"terminal": entry["id"]}
                if not attached and status in ("starting", "running", "stopping"):
                    pid = entry.get("pid")
                    current = process_identity(int(pid)) if pid is not None else None
                    saved = entry.get("process_identity")
                    if current is not None and (not saved or current == saved):
                        raise ValueError(
                            "An orphaned session process is still running; "
                            "resolve it before reopening"
                        )
                    entry["status"] = "interrupted"
                    entry.pop("pid", None)
                    entry.pop("process_identity", None)
                    self.state.save()
                self.inactive(entry)
                await self.terminals.restart(entry["id"])
                return {"terminal": entry["id"]}
            terminal_id = await self.terminals.create(
                harness,
                owner="custom",
                title="Configure " + harness,
                role="config",
            )
            return {"terminal": terminal_id}
        elif op == "session_create":
            group_id = self.group(str(request.get("group") or ""))
            title = display_name(request.get("title") or "New session")
            harness = str(
                request.get("harness") or self.state.config["default_harness"]
            )
            if not available(harness):
                raise ValueError("Independent session harness unavailable: " + harness)
            prompt = request.get("prompt") or ""
            native_session = request.get("native_session") or None
            if not isinstance(prompt, str) or len(prompt.encode()) > 16000:
                raise ValueError("Session prompt must be text of at most 16000 bytes")
            if native_session is not None and not isinstance(native_session, str):
                raise ValueError("Native session ID must be text")
            terminal_id = await self.terminals.create(
                harness,
                owner="custom",
                group=group_id,
                title=title,
                prompt=prompt,
                native_session=native_session,
            )
            return {"terminal": terminal_id}
        elif op in ("session_update", "session_restart", "session_delete"):
            terminal_id = str(request.get("terminal") or "")
            entry = self.custom(terminal_id)
            if op == "session_update":
                title = display_name(request.get("title"))
                group_id = self.group(str(request.get("group") or ""))
                entry.update(title=title, group=group_id)
            elif op == "session_restart":
                self.inactive(entry)
                if not available(entry["harness"]):
                    raise ValueError(
                        "Independent session harness unavailable: " + entry["harness"]
                    )
                await self.terminals.restart(terminal_id)
                return {"terminal": terminal_id}
            else:
                self.inactive(entry)
                if request.get("confirmed") is not True:
                    raise ValueError("Explicit session removal confirmation required")
                session = self.terminals.sessions.pop(terminal_id, None)
                if session is not None:
                    session.close()
                del self.terminals.entries[terminal_id]
            result = {}
        else:
            raise ValueError("Unsupported session/group command")
        self.state.save()
        return result

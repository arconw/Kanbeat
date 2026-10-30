from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable
import uuid

from .claude_runtime import ClaudeRuntimeError
from .codex_host import CodexHostError, CodexRpcError, thread_missing
from .native import available, process_identity
from .opencode_host import OpenCodeHostError, OpenCodeHttpError
from .state import LocalState
from .terminals import Terminals


def display_name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 64:
        raise ValueError("Name must contain 1..64 characters")
    if any(ord(char) < 32 for char in value):
        raise ValueError("Name cannot contain control characters")
    return value.strip()


class SessionGroups:
    def __init__(
        self,
        state: LocalState,
        terminals: Terminals,
        opencode_host: Callable[[], Awaitable[Any]] | None = None,
        codex_host: Callable[[], Awaitable[Any]] | None = None,
        claude_runtime: Any = None,
    ):
        self.state = state
        self.terminals = terminals
        self.opencode_host = opencode_host
        self.codex_host = codex_host
        self.claude_runtime = claude_runtime
        self.config_lock = asyncio.Lock()
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

    def config_entry(self, harness: str) -> dict[str, Any] | None:
        matches = [
            entry
            for entry in self.terminals.entries.values()
            if entry.get("owner") == "custom"
            and entry.get("role") == "config"
            and entry.get("harness") == harness
        ]
        return next(
            (
                row
                for row in reversed(matches)
                if row.get("status") in ("starting", "running", "stopping")
            ),
            matches[-1] if matches else None,
        )

    def opencode_config_entry(self) -> dict[str, Any] | None:
        return self.config_entry("opencode")

    async def open_opencode_config(self) -> dict[str, Any]:
        async with self.config_lock:
            return await self._open_opencode_config()

    async def _open_opencode_config(self) -> dict[str, Any]:
        if self.opencode_host is None:
            raise ValueError("OpenCode host provider is unavailable")
        entry = self.opencode_config_entry()
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
            try:
                host = await self.opencode_host()
            except (OpenCodeHostError, OSError) as error:
                raise ValueError(f"Could not start OpenCode host: {error}") from error
            session_id = entry.get("native_session")
            if not session_id or not await self.opencode_session_exists(
                host, session_id
            ):
                metadata = await self.create_opencode_session(host)
                session_id = metadata.session_id
                entry["native_session"] = session_id
                self.state.save()
            await self.terminals.restart(
                entry["id"],
                argv=host.viewer_argv(session_id),
                env=host.child_env(),
            )
            return {"terminal": entry["id"]}
        try:
            host = await self.opencode_host()
        except (OpenCodeHostError, OSError) as error:
            raise ValueError(f"Could not start OpenCode host: {error}") from error
        metadata = await self.create_opencode_session(host)
        terminal_id = await self.terminals.create(
            "opencode",
            owner="custom",
            title="Configure opencode",
            role="config",
            native_session=metadata.session_id,
            argv=host.viewer_argv(metadata.session_id),
            env=host.child_env(),
        )
        return {"terminal": terminal_id}

    async def open_codex_config(self) -> dict[str, Any]:
        async with self.config_lock:
            return await self._open_codex_config()

    async def _open_codex_config(self) -> dict[str, Any]:
        if self.codex_host is None:
            raise ValueError("Codex host provider is unavailable")
        entry = self.config_entry("codex")
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
            try:
                host = await self.codex_host()
            except (CodexHostError, OSError) as error:
                raise ValueError(f"Could not start Codex host: {error}") from error
            session_id = entry.get("native_session")
            if not session_id or not await self.codex_thread_exists(host, session_id):
                metadata = await self.create_codex_thread(host)
                session_id = metadata.thread_id
                entry["native_session"] = session_id
                self.state.save()
            await self.terminals.restart(
                entry["id"],
                argv=host.tui_argv(thread_id=session_id),
                env=host.child_env(),
            )
            return {"terminal": entry["id"]}
        try:
            host = await self.codex_host()
        except (CodexHostError, OSError) as error:
            raise ValueError(f"Could not start Codex host: {error}") from error
        metadata = await self.create_codex_thread(host)
        terminal_id = await self.terminals.create(
            "codex",
            owner="custom",
            title="Configure codex",
            role="config",
            native_session=metadata.thread_id,
            argv=host.tui_argv(thread_id=metadata.thread_id),
            env=host.child_env(),
        )
        return {"terminal": terminal_id}

    async def open_claude_config(self) -> dict[str, Any]:
        async with self.config_lock:
            return await self._open_claude_config()

    async def _open_claude_config(self) -> dict[str, Any]:
        runtime = self.claude_runtime
        if runtime is None:
            raise ValueError("Claude runtime provider is unavailable")
        entry = self.config_entry("claude")
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
            events_id = entry.get("events_id")
            if not isinstance(events_id, str) or not events_id:
                events_id = "config-" + uuid.uuid4().hex[:24]
                entry["events_id"] = events_id
            session_id = entry.get("native_session")
            try:
                if not session_id:
                    session_id = await runtime.start_config(events_id)
                    entry["native_session"] = session_id
                    self.state.save()
                argv = await runtime.attach_argv(session_id, events_id)
            except (ClaudeRuntimeError, OSError) as error:
                raise ValueError(
                    f"Could not open Claude config session: {error}"
                ) from error
            await self.terminals.restart(
                entry["id"], argv=argv, env=runtime.child_env()
            )
            return {"terminal": entry["id"]}
        events_id = "config-" + uuid.uuid4().hex[:24]
        try:
            session_id = await runtime.start_config(events_id)
            argv = await runtime.attach_argv(session_id, events_id)
        except (ClaudeRuntimeError, OSError) as error:
            raise ValueError(
                f"Could not open Claude config session: {error}"
            ) from error
        terminal_id = await self.terminals.create(
            "claude",
            owner="custom",
            title="Configure claude",
            role="config",
            native_session=session_id,
            argv=argv,
            env=runtime.child_env(),
        )
        self.terminals.entries[terminal_id]["events_id"] = events_id
        self.state.save()
        return {"terminal": terminal_id}

    async def create_codex_thread(self, host: Any) -> Any:
        try:
            metadata = await host.start_thread(self.state.board.project)
            await host.set_thread_name(metadata.thread_id, "Kanbeat config")
            return metadata
        except (CodexHostError, OSError) as error:
            raise ValueError(
                f"Could not create Codex config thread: {error}"
            ) from error

    @staticmethod
    async def codex_thread_exists(host: Any, session_id: str) -> bool:
        try:
            await host.observe_thread(session_id)
        except CodexRpcError as error:
            if thread_missing(error):
                return False
            raise ValueError(f"Could not read Codex config thread: {error}") from error
        except (CodexHostError, OSError) as error:
            raise ValueError(f"Could not read Codex config thread: {error}") from error
        return True

    async def create_opencode_session(self, host: Any) -> Any:
        try:
            return await host.create_session(directory=self.state.board.project)
        except (OpenCodeHostError, OSError) as error:
            raise ValueError(
                f"Could not create OpenCode config session: {error}"
            ) from error

    @staticmethod
    async def opencode_session_exists(host: Any, session_id: str) -> bool:
        try:
            await host.read_session(session_id)
        except OpenCodeHttpError as error:
            if error.status == 404:
                return False
            raise ValueError(
                f"Could not read OpenCode config session: {error}"
            ) from error
        except (OpenCodeHostError, OSError) as error:
            raise ValueError(
                f"Could not read OpenCode config session: {error}"
            ) from error
        return True

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
            if harness == "opencode" and self.opencode_host is not None:
                return await self.open_opencode_config()
            if harness == "codex" and self.codex_host is not None:
                return await self.open_codex_config()
            if harness == "claude" and self.claude_runtime is not None:
                return await self.open_claude_config()
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
                if entry.get("role") == "config":
                    return await self.dispatch(
                        {
                            "op": "session_config_open",
                            "harness": entry.get("harness") or "",
                        }
                    )
                await self.terminals.restart(terminal_id)
                return {"terminal": terminal_id}
            else:
                self.inactive(entry)
                if request.get("confirmed") is not True:
                    raise ValueError("Explicit session removal confirmation required")
                if (
                    entry.get("role") == "config"
                    and entry.get("harness") == "claude"
                    and self.claude_runtime is not None
                ):
                    session_id = entry.get("native_session")
                    if isinstance(session_id, str) and session_id:
                        try:
                            await self.claude_runtime.stop_session(session_id)
                        except (ClaudeRuntimeError, OSError):
                            pass
                session = self.terminals.sessions.pop(terminal_id, None)
                if session is not None:
                    session.close()
                del self.terminals.entries[terminal_id]
            result = {}
        else:
            raise ValueError("Unsupported session/group command")
        self.state.save()
        return result

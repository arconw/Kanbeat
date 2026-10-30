from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import tempfile
import uuid
from typing import Any, Callable

from .launcher import app_command, external_environment
from .native import Outcome
from .native_events import claude_settings_args, events_path, read_events
from .native_settings import NativeSettings, SettingsError, cli_args

COMMAND_TIMEOUT = 30.0
ATTACH_TIMEOUT = 30.0
BACKGROUND_TIMEOUT = 60.0
BACKGROUND_ID = re.compile(r"backgrounded\s*.\s*([0-9a-f]{8})")
IDLE_GRACE = 5.0
FAILURE_GRACE = 5.0
STARTUP_GRACE = 30.0
MAX_ROWS = 500
MAX_MESSAGE = 1_000_000


class ClaudeRuntimeError(RuntimeError):
    pass


class ClaudeRuntime:
    def __init__(
        self,
        project: Path,
        state_dir: Path,
        *,
        env: dict[str, str] | None = None,
        executable: str = "claude",
        poll_interval: float = 0.5,
    ):
        self.project = Path(project)
        self.state_dir = Path(state_dir)
        self.events_dir = self.state_dir / "claude-events"
        self.base_env = dict(os.environ if env is None else env)
        self.executable = executable
        self.poll_interval = poll_interval
        self.sessions: set[str] = set()
        self._config_states: dict[str, dict[str, Any]] = {}

    def child_env(self) -> dict[str, str]:
        return external_environment(self.base_env)

    def settings_args(self, events_id: str) -> list[str]:
        return claude_settings_args(
            app_command("--native-bridge"), self.events_dir, events_id
        )

    async def launch(
        self, args: list[str], *, timeout: float = COMMAND_TIMEOUT
    ) -> tuple[int, str, str]:
        process = await asyncio.create_subprocess_exec(
            self.executable,
            *args,
            cwd=self.project,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.child_env(),
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError as error:
            process.kill()
            await process.wait()
            raise ClaudeRuntimeError("Claude command timed out") from error
        return (
            int(process.returncode or 0),
            stdout.decode("utf-8", "replace"),
            stderr.decode("utf-8", "replace"),
        )

    async def launch_background(
        self, args: list[str], *, timeout: float = BACKGROUND_TIMEOUT
    ) -> tuple[int, str]:
        descriptor, path = tempfile.mkstemp(
            dir=self.state_dir, prefix=".claude-bg-", suffix=".log"
        )
        try:
            os.chmod(path, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                process = await asyncio.create_subprocess_exec(
                    self.executable,
                    *args,
                    cwd=self.project,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=output,
                    stderr=asyncio.subprocess.STDOUT,
                    env=self.child_env(),
                    start_new_session=True,
                )
                try:
                    await asyncio.wait_for(process.wait(), timeout=timeout)
                except asyncio.TimeoutError as error:
                    process.kill()
                    await process.wait()
                    raise ClaudeRuntimeError(
                        "Claude background command timed out"
                    ) from error
                code = int(process.returncode or 0)
            text = Path(path).read_text(encoding="utf-8", errors="replace")
            return code, text
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    @staticmethod
    def background_id(output: str) -> str | None:
        match = BACKGROUND_ID.search(output)
        return match.group(1) if match else None

    async def wait_short(self, short: str, timeout: float) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            for row in await self.agents():
                if row.get("id") == short:
                    return row
            if loop.time() >= deadline:
                raise ClaudeRuntimeError("Claude background session did not appear")
            await asyncio.sleep(self.poll_interval)

    async def agents(self) -> list[dict[str, Any]]:
        args = ["agents", "--json", "--all", "--cwd", str(self.project)]
        code, stdout, stderr = await self.launch(args, timeout=15)
        if code != 0:
            fallback = ["agents", "--json", "--all"]
            code, stdout, stderr = await self.launch(fallback, timeout=15)
            if code != 0:
                raise ClaudeRuntimeError(
                    "Could not list Claude sessions: "
                    + (stderr or stdout).strip()[:300]
                )
        try:
            rows = json.loads(stdout)
        except ValueError as error:
            raise ClaudeRuntimeError("Claude agents returned invalid JSON") from error
        if not isinstance(rows, list):
            raise ClaudeRuntimeError("Claude agents returned an unexpected payload")
        result = []
        for row in rows[:MAX_ROWS]:
            if isinstance(row, dict) and isinstance(row.get("sessionId"), str):
                result.append(row)
        return result

    async def find(self, session: str) -> dict[str, Any] | None:
        for row in await self.agents():
            if row.get("sessionId") == session or row.get("id") == session:
                if row.get("kind") == "background":
                    return row
        return None

    @staticmethod
    def activity(row: dict[str, Any] | None) -> str | None:
        if row is None:
            return None
        status = str(row.get("status") or "")
        state = str(row.get("state") or "")
        if state in ("failed", "error"):
            return "error"
        if state == "blocked":
            return "waiting"
        if status == "busy":
            return "working"
        if status == "waiting":
            return "waiting"
        if status == "idle":
            return "idle"
        if state == "working":
            return "working"
        return None

    def track(self, session: str) -> None:
        self.sessions.add(session)

    def untrack(self, session: str) -> None:
        self.sessions.discard(session)

    async def stop_session(self, session: str) -> bool:
        row = await self.find(session)
        if row is None:
            self.untrack(session)
            return True
        short = row.get("id")
        if not isinstance(short, str) or not short:
            return False
        try:
            code, _stdout, _stderr = await self.launch(["stop", short], timeout=20)
        except ClaudeRuntimeError:
            return False
        if code == 0:
            for _ in range(20):
                current = await self.find(session)
                if current is None or current.get("state") == "stopped":
                    break
                await asyncio.sleep(0.1)
            self.untrack(session)
            return True
        return False

    async def wait_running(self, session: str, timeout: float) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            row = await self.find(session)
            if row is not None:
                return row
            if loop.time() >= deadline:
                raise ClaudeRuntimeError("Claude background session did not start")
            await asyncio.sleep(self.poll_interval)

    async def resume_idle(self, session: str, events_id: str) -> dict[str, Any]:
        args = ["--bg", "--resume", session, *self.settings_args(events_id)]
        code, stdout, stderr = await self.launch(args)
        if code != 0:
            raise ClaudeRuntimeError(
                "Could not resume Claude session: " + (stderr or stdout).strip()[:300]
            )
        self.track(session)
        return await self.wait_running(session, ATTACH_TIMEOUT)

    async def attach_argv(self, session: str, events_id: str) -> list[str]:
        row = await self.find(session)
        if row is None or row.get("state") == "stopped":
            row = await self.resume_idle(session, events_id)
        short = row.get("id")
        if not isinstance(short, str) or not short:
            raise ClaudeRuntimeError("Claude session has no attachable id")
        return [self.executable, "attach", short]

    async def start_config(self, events_id: str) -> str:
        args = ["--bg", *self.settings_args(events_id)]
        code, output = await self.launch_background(args)
        if code != 0:
            raise ClaudeRuntimeError(
                "Could not start Claude config session: " + output.strip()[:300]
            )
        short = self.background_id(output)
        if short is None:
            raise ClaudeRuntimeError("Claude did not report a background session")
        row = await self.wait_short(short, ATTACH_TIMEOUT)
        session = row.get("sessionId")
        if not isinstance(session, str) or not session:
            raise ClaudeRuntimeError("Claude config session has no session id")
        self.track(session)
        return session

    def config_state(self, events_id: str) -> tuple[NativeSettings | None, str | None]:
        records, _offset = read_events(events_path(self.events_dir, events_id))
        model = None
        effort = None
        activity = None
        for record in records:
            event = record.get("event")
            if event == "Statusline":
                model = record.get("model") or model
                effort = record.get("effort") or effort
            elif event == "SessionStart":
                model = record.get("model") or model
                activity = None
            elif event in ("Stop", "StopFailure"):
                effort = record.get("effort") or effort
                activity = "error" if event == "StopFailure" else "idle"
            elif record.get("activity") in ("working", "waiting", "error"):
                activity = str(record["activity"])
            elif record.get("activity") == "tool":
                activity = "working"
            elif record.get("activity") in ("done", "started"):
                activity = "idle" if record.get("activity") == "done" else None
            elif record.get("activity") == "stopped":
                activity = None
        settings = None
        if model or effort:
            try:
                settings = NativeSettings("claude", model, None, effort)
            except SettingsError:
                settings = None
        return settings, activity

    async def close(self) -> None:
        sessions = list(self.sessions)
        self.sessions.clear()
        for session in sessions:
            try:
                await self.stop_session(session)
            except (ClaudeRuntimeError, OSError, asyncio.TimeoutError):
                continue


class ClaudeRun:
    def __init__(
        self,
        runtime: ClaudeRuntime,
        prompt: str,
        session_id: str | None,
        log_path: Path,
        *,
        settings: NativeSettings | None = None,
        on_session: Callable[[str], None] | None = None,
        on_activity: Callable[[str | None], None] | None = None,
    ):
        self.runtime = runtime
        self.prompt = prompt
        self.session_id = session_id
        self.log_path = log_path
        self.settings = settings
        self.on_session = on_session or (lambda _value: None)
        self.on_activity = on_activity or (lambda _value: None)
        self.stopping = False
        self.activity: str | None = None
        self.answer = ""
        self.error = ""

    def activity_update(self, value: str | None) -> None:
        if value != self.activity:
            self.activity = value
            self.on_activity(value)

    async def stop(self) -> None:
        self.stopping = True
        if self.session_id:
            try:
                await self.runtime.stop_session(self.session_id)
            except (ClaudeRuntimeError, OSError, asyncio.TimeoutError):
                pass

    async def execute(self) -> Outcome:
        if self.stopping:
            return Outcome(False, "interrupted before start", self.session_id, "")
        resumed = bool(self.session_id)
        events_id = "run-" + uuid.uuid4().hex[:24]
        args = ["--bg"]
        if resumed:
            try:
                row = await self.runtime.find(self.session_id)
                if row is not None and (
                    row.get("status") in ("busy", "waiting")
                    or row.get("state") == "blocked"
                ):
                    await self.runtime.stop_session(self.session_id)
            except (ClaudeRuntimeError, OSError):
                pass
            args += ["--resume", self.session_id]
        args += self.runtime.settings_args(events_id)
        if not resumed and self.settings is not None:
            args += cli_args("claude", self.settings)
        args.append(self.prompt)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as log:
            log.write("[Kanbeat] Claude background turn. Starting.\n")
            log.flush()
            try:
                code, output = await self.runtime.launch_background(args)
            except (ClaudeRuntimeError, OSError) as error:
                return Outcome(
                    False, f"Claude launch failed: {error}", self.session_id, ""
                )
            if code != 0:
                reason = output.strip()[:400] or f"exit={code}"
                log.write(reason + "\n")
                return Outcome(
                    False, "Claude launch failed: " + reason, self.session_id, ""
                )
            short = self.runtime.background_id(output)
            if short is None:
                return Outcome(
                    False,
                    "Claude did not report a background session",
                    self.session_id,
                    "",
                )
            if not resumed:
                try:
                    row = await self.runtime.wait_short(short, ATTACH_TIMEOUT)
                except (ClaudeRuntimeError, OSError) as error:
                    return Outcome(False, str(error), self.session_id, "")
                session = row.get("sessionId")
                if not isinstance(session, str) or not session:
                    return Outcome(
                        False, "Claude background session has no session id", None, ""
                    )
                self.session_id = session
                self.on_session(session)
            self.runtime.track(self.session_id)
            assert self.session_id is not None
            if self.stopping:
                try:
                    await self.runtime.stop_session(self.session_id)
                except (ClaudeRuntimeError, OSError, asyncio.TimeoutError):
                    pass
                return Outcome(False, "interrupted by board Stop", self.session_id, "")
            loop = asyncio.get_running_loop()
            startup_deadline = loop.time() + STARTUP_GRACE
            idle_deadline: float | None = None
            offset = 0
            seen = False
            saw_activity = False
            session_ended = False
            last_stop: dict[str, Any] | None = None
            failure: dict[str, Any] | None = None
            failure_deadline: float | None = None
            row: dict[str, Any] | None = None
            try:
                while True:
                    if self.stopping:
                        await self.stop()
                        return Outcome(
                            False,
                            "interrupted by board Stop",
                            self.session_id,
                            self.answer,
                        )
                    try:
                        row = await self.runtime.find(self.session_id)
                    except (ClaudeRuntimeError, OSError):
                        row = None
                    if row is not None:
                        seen = True
                        self.activity_update(self.runtime.activity(row))
                        if (
                            row.get("state") == "failed"
                            and last_stop is None
                            and saw_activity
                        ):
                            failure = {"error": "Claude session failed"}
                        if row.get("status") in ("busy", "waiting") or row.get(
                            "state"
                        ) in ("working", "blocked"):
                            saw_activity = True
                    records, offset = read_events(
                        events_path(self.runtime.events_dir, events_id), offset
                    )
                    for record in records:
                        event = record.get("event")
                        if record.get("session_id") not in (None, self.session_id):
                            continue
                        if event in (
                            "UserPromptSubmit",
                            "PreToolUse",
                            "PostToolUse",
                            "Notification",
                        ):
                            saw_activity = True
                        elif event == "Stop":
                            saw_activity = True
                            last_stop = record
                            failure = None
                            failure_deadline = None
                        elif event == "StopFailure":
                            saw_activity = True
                            failure = record
                        elif event == "SessionEnd":
                            saw_activity = True
                            session_ended = True
                            if last_stop is None:
                                failure = {"error": "Claude session ended"}
                    if last_stop is not None and session_ended:
                        break
                    status = str((row or {}).get("status") or "")
                    if status == "idle":
                        if last_stop is not None or failure is not None:
                            break
                        if saw_activity:
                            if idle_deadline is None:
                                idle_deadline = loop.time() + IDLE_GRACE
                            if loop.time() >= idle_deadline:
                                failure = {
                                    "error": "Claude reported idle without a completion event"
                                }
                                break
                        elif loop.time() >= startup_deadline:
                            failure = {"error": "Claude turn did not start"}
                            break
                    else:
                        idle_deadline = None
                    if failure is not None:
                        if failure_deadline is None:
                            failure_deadline = loop.time() + FAILURE_GRACE
                        if (
                            status not in ("busy", "waiting")
                            or loop.time() >= failure_deadline
                        ):
                            break
                    if not seen and loop.time() >= startup_deadline:
                        if last_stop is None:
                            failure = failure or {
                                "error": "Claude background session did not start"
                            }
                        break
                    if seen and row is None and loop.time() >= startup_deadline:
                        if last_stop is None:
                            failure = failure or {"error": "Claude session disappeared"}
                        break
                    await asyncio.sleep(self.runtime.poll_interval)
                if failure is not None:
                    self.error = str(
                        failure.get("error")
                        or failure.get("message")
                        or "Claude turn failed"
                    )[:400]
                    self.activity_update("error")
                    log.write(self.error + "\n")
                    return Outcome(False, self.error, self.session_id, self.answer)
                assert last_stop is not None
                self.answer = str(last_stop.get("message") or "")[:MAX_MESSAGE]
                self.activity_update("idle")
                if self.answer:
                    log.write(self.answer[-3000:] + "\n")
                return Outcome(True, "completed", self.session_id, self.answer)
            finally:
                self.activity_update(None)

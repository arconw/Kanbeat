"""Kanbeat native CLI worker: structured event stream and resume identifiers.

The CLI still owns its model, credentials, tools and sandbox settings.
This is headless task execution, not an embedded interactive terminal.
"""

from __future__ import annotations

from dataclasses import dataclass
import asyncio
import json
import logging
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
from typing import Any, Callable

from .launcher import external_environment
from .native_settings import NativeSettings, SettingsError, cli_args, native_id

HARNESS = {"codex": "codex", "claude": "claude", "opencode": "opencode", "pi": "pi"}
VERDICT = re.compile(r"(?m)^KANBEAT_VERDICT:\s*(approve|changes_requested)\s*$")
LOGGER = logging.getLogger(__name__)


def available(name: str) -> bool:
    return name in HARNESS and shutil.which(HARNESS[name]) is not None


def command(
    name: str,
    prompt: str,
    session_id: str | None,
    settings: NativeSettings | None = None,
) -> tuple[list[str], bytes | None]:
    """Native CLI argv; ticket contents are never interpolated into a shell."""
    extra = cli_args(name, settings)
    session_id = native_id(session_id)
    if name == "codex":
        if session_id:
            return [
                "codex",
                "exec",
                "resume",
                *extra,
                session_id,
                "-",
                "--json",
            ], prompt.encode()
        return ["codex", "exec", *extra, "--json", "-"], prompt.encode()
    if name == "claude":
        argv = [
            "claude",
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            *extra,
        ]
        if session_id:
            argv += ["--resume", session_id]
        return argv, None
    if name == "opencode":
        argv = ["opencode", "run", "--format", "json", *extra]
        if session_id:
            argv += ["--session", session_id]
        argv.append(prompt)
        return argv, None
    if name == "pi":
        argv = ["pi", "--mode", "json", *extra]
        if session_id:
            argv += ["--session", session_id]
        argv.append(prompt)
        return argv, None
    raise ValueError("Unsupported harness: " + name)


@dataclass
class Outcome:
    ok: bool
    reason: str
    session_id: str | None
    answer: str
    resume_missing: bool = False


class EventStream:
    def __init__(self, harness: str):
        self.harness = harness
        self.session_id: str | None = None
        self.answer = ""
        self.finished = False
        self.failed = False
        self.error = ""
        self.events = 0
        self.activity: str | None = None

    def push(self, event: dict[str, Any]) -> None:
        self.events += 1
        kind = str(event.get("type", ""))
        if self.harness == "codex":
            if kind == "thread.started":
                self.session_id = str(event.get("thread_id") or "") or self.session_id
            if kind == "item.completed":
                item = event.get("item") or {}
                if isinstance(item, dict) and item.get("type") == "agent_message":
                    self.answer = str(item.get("text") or self.answer)
            if kind in ("turn.started", "item.started"):
                self.activity = "working"
            if kind == "turn.completed":
                self.finished = True
                self.activity = "idle"
            if kind in ("turn.failed", "error"):
                self.failed = True
                self.activity = "error"
                self.error = str(event.get("message") or event.get("error") or kind)
        elif self.harness == "claude":
            sid = event.get("session_id")
            if isinstance(sid, str) and sid:
                self.session_id = sid
            if kind in ("assistant", "user"):
                self.activity = "working"
            if kind == "result":
                self.finished = True
                self.failed = (
                    bool(event.get("is_error")) or event.get("subtype") != "success"
                )
                self.answer = str(event.get("result") or "")
                self.activity = "error" if self.failed else "idle"
                if self.failed:
                    self.error = self.answer[:400]
        elif self.harness == "opencode":
            sid = event.get("sessionID") or event.get("session_id")
            if isinstance(sid, str) and sid:
                self.session_id = sid
            part = event.get("part") or {}
            if kind == "text" and isinstance(part, dict):
                self.answer = str(part.get("text") or self.answer)
            if kind in ("step_start", "text"):
                self.activity = "working"
            if kind == "step_finish":
                reason = part.get("reason") if isinstance(part, dict) else None
                if reason != "tool-calls":
                    self.activity = "idle"
            if kind in (
                "permission.asked",
                "permission.v2.asked",
                "question.asked",
                "question.v2.asked",
            ):
                self.activity = "waiting"
            if kind in (
                "permission.replied",
                "permission.v2.replied",
                "question.replied",
                "question.rejected",
                "question.v2.replied",
                "question.v2.rejected",
            ):
                self.activity = "working"
            if kind == "error":
                self.failed = True
                self.activity = "error"
                self.error = str(event.get("error") or "OpenCode emitted error")[:400]
            # OpenCode run exits after its turn; no universal end event across versions.
        elif self.harness == "pi":
            sid = event.get("sessionId") or event.get("session_id")
            if kind == "session":
                sid = event.get("id") or sid
            if isinstance(sid, str) and sid:
                self.session_id = sid
            if kind == "session" and isinstance(event.get("session"), dict):
                sid = event["session"].get("id")
                if isinstance(sid, str):
                    self.session_id = sid
            if kind in ("message_end", "turn_end"):
                msg = event.get("message") or {}
                if isinstance(msg, dict) and msg.get("role") == "assistant":
                    chunks = msg.get("content") or []
                    self.answer = (
                        "\n".join(
                            str(c.get("text"))
                            for c in chunks
                            if isinstance(c, dict) and c.get("type") == "text"
                        )
                        or self.answer
                    )
                    if msg.get("stopReason") in ("error", "aborted"):
                        self.failed = True
                        self.error = str(
                            msg.get("errorMessage")
                            or "Pi assistant " + msg["stopReason"]
                        )[:400]
            if kind in ("agent_start", "message_start", "tool_execution_start"):
                self.activity = "working"
            if kind in ("message_end", "turn_end"):
                self.activity = "error" if self.failed else "working"
            if kind in ("agent_end", "agent_settled"):
                self.finished = True
                if event.get("aborted"):
                    self.failed = True
                    self.error = "Pi run aborted"
                self.activity = "error" if self.failed else "idle"

    def result(self, return_code: int, stderr: str) -> Outcome:
        error_text = stderr + " " + self.error
        missing = bool(
            re.search(
                r"(?:session|thread).{0,90}"
                r"(?:not found|does not exist|unknown|missing|cannot resume|no such)",
                error_text,
                re.I,
            )
            or re.search(
                r"(?:not found|cannot find|could not find|no such|unknown)"
                r".{0,90}(?:session|thread)",
                error_text,
                re.I,
            )
        )
        ok = (
            return_code == 0
            and not self.failed
            and self.events > 0
            and (self.finished or self.harness == "opencode")
        )
        reason = (
            "completed"
            if ok
            else (
                self.error
                or stderr.strip()[-400:]
                or f"exit={return_code}, no completion event"
            )
        )
        return Outcome(ok, reason, self.session_id, self.answer, missing)


def process_identity(pid: int) -> str | None:
    """OS-level birth fingerprint used before stopping a crash-orphaned worker.

    Never kill an arbitrary reused PID. Linux uses /proc start ticks; other
    POSIX systems use the process start time reported by ps.
    """
    try:
        stat = Path(f"/proc/{pid}/stat")
        if stat.exists():
            fields = stat.read_text(encoding="utf-8").rsplit(") ", 1)[1].split()
            return "linux:" + fields[19]
        output = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "lstart="],
            stderr=subprocess.DEVNULL,
            timeout=2,
            text=True,
        ).strip()
        return "ps:" + output if output else None
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def signal_owned_process(proc: asyncio.subprocess.Process, signum: int) -> None:
    """Signal only the current child-led group, otherwise the child itself."""
    if proc.returncode is not None:
        return
    try:
        owned_group = os.getpgid(proc.pid) == proc.pid
    except (ProcessLookupError, PermissionError):
        LOGGER.debug("Cannot verify group of tracked process %s", proc.pid)
        owned_group = False
    if owned_group:
        try:
            os.killpg(proc.pid, signum)
            return
        except (ProcessLookupError, PermissionError):
            # macOS may deny group signals after PTY hangup. Never retry the
            # group identifier; the still-owned child can be signaled directly.
            pass
    try:
        proc.send_signal(signum)
    except (ProcessLookupError, PermissionError):
        # A denied direct signal must not abort escalation and reaping.
        pass


async def terminate_process(proc: asyncio.subprocess.Process | None) -> None:
    if not proc or proc.returncode is not None:
        return
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal_owned_process(proc, signum)
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
            return
        except asyncio.TimeoutError:
            continue
    if proc.returncode is None:
        signal_owned_process(proc, signal.SIGKILL)
        await proc.wait()


class NativeRun:
    def __init__(
        self,
        harness: str,
        prompt: str,
        session_id: str | None,
        cwd: Path,
        log_path: Path,
        on_session: Callable[[str], None] | None = None,
        on_process: Callable[[int, str | None], None] | None = None,
        on_activity: Callable[[str | None], None] | None = None,
        command_builder: Callable[
            [str, str, str | None], tuple[list[str], bytes | None]
        ]
        | None = None,
        env: dict[str, str] | None = None,
        settings: NativeSettings | None = None,
    ):
        self.harness = harness
        self.prompt = prompt
        self.session_id = session_id
        self.cwd = cwd
        self.log_path = log_path
        self.on_session = on_session or (lambda _value: None)
        self.on_process = on_process or (lambda _pid, _identity: None)
        self.on_activity = on_activity or (lambda _value: None)
        self.command_builder = command_builder
        self.env = env
        self.settings = settings
        self.process: asyncio.subprocess.Process | None = None
        self.stopping = False

    async def stop(self) -> None:
        self.stopping = True
        await self._terminate()

    async def _terminate(self) -> None:
        await terminate_process(self.process)

    async def execute(self) -> Outcome:
        if self.stopping:
            return Outcome(False, "interrupted before start", self.session_id, "")
        builder = self.command_builder
        try:
            if builder is None:
                args, stdin_payload = command(
                    self.harness, self.prompt, self.session_id, self.settings
                )
            else:
                args, stdin_payload = builder(
                    self.harness, self.prompt, self.session_id
                )
        except SettingsError as exc:
            return Outcome(
                False, f"Invalid native settings: {exc}", self.session_id, ""
            )
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        stream = EventStream(self.harness)
        error_bytes = bytearray()
        with self.log_path.open("a", encoding="utf-8") as log:
            log.write(f"\n[Kanbeat] Harness: {self.harness}. Starting native CLI.\n")
            log.flush()
            try:
                self.process = await asyncio.create_subprocess_exec(
                    *args,
                    cwd=self.cwd,
                    stdin=asyncio.subprocess.PIPE
                    if stdin_payload is not None
                    else asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                    env=self.env if self.env is not None else external_environment(),
                    limit=2 * 1024 * 1024,
                )
            except OSError as exc:
                return Outcome(False, str(exc), self.session_id, "")
            proc = self.process

            async def read_stderr() -> None:
                assert proc.stderr is not None
                while chunk := await proc.stderr.read(4096):
                    if len(error_bytes) < 8192:
                        error_bytes.extend(chunk[: 8192 - len(error_bytes)])
                    log.write(chunk.decode("utf-8", "replace"))
                    log.flush()

            stderr_task = asyncio.create_task(read_stderr())

            async def drain_stdout() -> None:
                assert proc.stdout is not None
                while await proc.stdout.read(65536):
                    pass

            activity = None
            try:
                self.on_process(proc.pid, process_identity(proc.pid))
                if self.stopping:
                    await self.stop()
                    return Outcome(
                        False, "interrupted during launch", self.session_id, ""
                    )
                if stdin_payload is not None and proc.stdin is not None:
                    proc.stdin.write(stdin_payload)
                    await proc.stdin.drain()
                    proc.stdin.close()
                assert proc.stdout is not None
                async for raw in proc.stdout:
                    try:
                        event = json.loads(raw)
                    except (ValueError, UnicodeError):
                        log.write(raw.decode("utf-8", "replace")[:2000])
                        continue
                    if not isinstance(event, dict):
                        continue
                    try:
                        stream.push(event)
                    except (ValueError, TypeError, KeyError):
                        continue
                    if stream.activity != activity:
                        activity = stream.activity
                        self.on_activity(activity)
                    if stream.session_id and stream.session_id != self.session_id:
                        self.session_id = stream.session_id
                        self.on_session(self.session_id)
                    if event.get("type") in (
                        "item.completed",
                        "text",
                        "result",
                        "message_end",
                        "error",
                    ):
                        summary = stream.answer or stream.error
                        if summary:
                            log.write(summary[-3000:] + "\n")
                            log.flush()
                code = await proc.wait()
                await stderr_task
                if self.stopping:
                    return Outcome(
                        False,
                        "interrupted by board Stop",
                        stream.session_id,
                        stream.answer,
                    )
                return stream.result(code, error_bytes.decode("utf-8", "replace"))
            except (OSError, ValueError) as exc:
                return Outcome(
                    False,
                    f"Native CLI transport error: {type(exc).__name__}: {exc}",
                    stream.session_id or self.session_id,
                    stream.answer,
                )
            finally:
                if proc.stdin is not None:
                    proc.stdin.close()
                drain_task = None
                try:
                    if proc.returncode is None:
                        drain_task = asyncio.create_task(drain_stdout())
                        await self._terminate()
                finally:
                    tasks = [stderr_task] + (
                        [drain_task] if drain_task is not None else []
                    )
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)

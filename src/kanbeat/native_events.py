"""Private Claude hook/statusline bridge with normalized JSONL events.

Only the JSON object on stdin is read. Transcripts, native configuration and
credentials are never opened, and the bridge writes nothing to stdout/stderr.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import IO, Any

from .native_settings import SettingsError, from_claude_statusline, scalar

MAX_INPUT_BYTES = 1024 * 1024
MAX_MESSAGE_CHARS = 32000
MAX_FILE_BYTES = 8 * 1024 * 1024
RECORD_VERSION = 1
TERMINAL_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
HOOK_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "Stop",
    "StopFailure",
    "Notification",
    "SessionEnd",
)
ACTIVITY = {
    "SessionStart": "started",
    "UserPromptSubmit": "working",
    "PreToolUse": "tool",
    "PostToolUse": "working",
    "Notification": "waiting",
    "Stop": "done",
    "StopFailure": "error",
    "SessionEnd": "stopped",
}
SUBCOMMANDS = ("hook", "statusline")


def events_path(events_dir: Path | str, terminal_id: str) -> Path:
    if not TERMINAL_ID.fullmatch(terminal_id):
        raise ValueError("Invalid terminal identifier")
    return Path(events_dir) / f"{terminal_id}.jsonl"


def bridge_command(
    argv_prefix: Sequence[str], events_dir: Path | str, terminal_id: str, kind: str
) -> str:
    if kind not in SUBCOMMANDS:
        raise ValueError("Unknown bridge command")
    events_path(events_dir, terminal_id)
    return shlex.join(
        [
            *argv_prefix,
            "--events-dir",
            str(events_dir),
            "--terminal-id",
            terminal_id,
            kind,
        ]
    )


def settings_json(
    argv_prefix: Sequence[str], events_dir: Path | str, terminal_id: str
) -> str:
    hook = {
        "type": "command",
        "command": bridge_command(argv_prefix, events_dir, terminal_id, "hook"),
        "timeout": 10,
    }
    config = {
        "hooks": {event: [{"hooks": [hook]}] for event in HOOK_EVENTS},
        "statusLine": {
            "type": "command",
            "command": bridge_command(
                argv_prefix, events_dir, terminal_id, "statusline"
            ),
            "padding": 0,
        },
    }
    return json.dumps(config, separators=(",", ":"))


def claude_settings_args(
    argv_prefix: Sequence[str], events_dir: Path | str, terminal_id: str
) -> list[str]:
    return ["--settings", settings_json(argv_prefix, events_dir, terminal_id)]


def _text(value: Any, *, tail: bool = False) -> str | None:
    if not isinstance(value, str):
        return None
    return value[-MAX_MESSAGE_CHARS:] if tail else value[:MAX_MESSAGE_CHARS]


def _identifier(value: Any) -> str | None:
    try:
        return scalar(value, "id")
    except SettingsError:
        return None


def normalize_hook(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    event = payload.get("hook_event_name")
    if event not in ACTIVITY:
        return None
    record: dict[str, Any] = {
        "event": event,
        "activity": ACTIVITY[event],
        "session_id": _identifier(payload.get("session_id")),
        "permission_mode": _identifier(payload.get("permission_mode")),
    }
    model = payload.get("model")
    model = model.get("id") if isinstance(model, dict) else model
    record["model"] = _identifier(model)
    effort = payload.get("effort")
    effort = effort.get("level") if isinstance(effort, dict) else effort
    record["effort"] = _identifier(effort)
    if event in ("PreToolUse", "PostToolUse"):
        record["tool"] = _identifier(payload.get("tool_name"))
    if event == "Notification":
        record["notification_type"] = _identifier(payload.get("notification_type"))
    if event in ("Stop", "StopFailure"):
        record["message"] = _text(payload.get("last_assistant_message"), tail=True)
        record["final"] = event == "Stop"
        record["stop_hook_active"] = payload.get("stop_hook_active") is True
        record["error"] = _text(payload.get("error"))
    return record


def normalize_statusline(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    try:
        settings = from_claude_statusline(payload)
    except SettingsError:
        return None
    session_id = _identifier(payload.get("session_id"))
    if not (session_id and (settings.model or settings.effort)):
        return None
    return {
        "event": "Statusline",
        "activity": "settings",
        "session_id": session_id,
        "model": settings.model,
        "effort": settings.effort,
    }


def read_payload(stream: IO[bytes], limit: int = MAX_INPUT_BYTES) -> Any:
    raw = stream.read(limit + 1)
    if len(raw) > limit:
        return None
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        return None


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def append_record(path: Path, record: dict[str, Any]) -> bool:
    _ensure_private_dir(path.parent)
    line = (
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode()
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        final = record.get("event") in ("Stop", "StopFailure")
        if os.fstat(fd).st_size + len(line) > MAX_FILE_BYTES and not final:
            return False
        os.write(fd, line)
        return True
    finally:
        os.close(fd)


def _write_state(path: Path, value: dict[str, Any]) -> None:
    _ensure_private_dir(path.parent)
    descriptor, temp = tempfile.mkstemp(dir=path.parent, prefix=".writing-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, separators=(",", ":"))
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def _settings_unchanged(path: Path, record: dict[str, Any]) -> bool:
    try:
        last = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    keys = ("session_id", "model", "effort")
    return isinstance(last, dict) and all(last.get(k) == record.get(k) for k in keys)


def record_event(
    kind: str, payload: Any, events_dir: Path | str, terminal_id: str
) -> bool:
    record = (
        normalize_hook(payload) if kind == "hook" else normalize_statusline(payload)
    )
    if record is None:
        return False
    path = events_path(events_dir, terminal_id)
    state = None
    if kind == "statusline":
        state = path.with_suffix(".settings.json")
        if _settings_unchanged(state, record):
            return False
    record = {
        "v": RECORD_VERSION,
        "time": round(time.time(), 3),
        "harness": "claude",
        "terminal_id": terminal_id,
        **record,
    }
    if not append_record(path, record):
        return False
    if state is not None:
        _write_state(state, {k: record[k] for k in ("session_id", "model", "effort")})
    return True


def read_events(path: Path | str, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    records: list[dict[str, Any]] = []
    try:
        with open(path, "rb") as stream:
            stream.seek(offset)
            data = stream.read()
    except OSError:
        return records, offset
    end = data.rfind(b"\n") + 1
    for line in data[:end].splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("v") == RECORD_VERSION:
            records.append(value)
    return records, offset + end


def main(argv: Sequence[str], stdin: IO[bytes] | None = None) -> int:
    try:
        args = list(argv)
        values: dict[str, str] = {}
        while len(args) >= 2 and args[0] in ("--events-dir", "--terminal-id"):
            values[args[0]] = args[1]
            args = args[2:]
        if len(args) != 1 or args[0] not in SUBCOMMANDS or len(values) != 2:
            return 0
        source = stdin if stdin is not None else sys.stdin.buffer
        payload = read_payload(source)
        record_event(args[0], payload, values["--events-dir"], values["--terminal-id"])
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
    return 0

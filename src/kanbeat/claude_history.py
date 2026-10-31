"""Read Claude Code transcripts into normalized conversation events.

The module is the Claude-specific reader for harness conversation transfer. It
reads a transcript only when called, never writes to it and never logs its
content. Records that are not original conversation are skipped, and content
the reader cannot export is reported through ``unsupported`` by category.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .session_transfer import (
    ROLE_TOOL_CALL,
    ROLE_TOOL_RESULT,
    ROLE_USER,
    ConversationEvent,
    HarnessHistory,
    TransferLedger,
    find_import_markers,
)

HARNESS = "claude"
HISTORY_AVAILABLE = "available"
HISTORY_MISSING = "missing"
HISTORY_UNREACHABLE = "unreachable"
HISTORY_OUTCOMES = (HISTORY_AVAILABLE, HISTORY_MISSING, HISTORY_UNREACHABLE)
HISTORY_STATUSES = HISTORY_OUTCOMES

UNSUPPORTED_HIDDEN_REASONING = "hidden reasoning"
UNSUPPORTED_IMAGES = "images"
UNSUPPORTED_ATTACHMENTS = "attachments"
UNSUPPORTED_COMPACTION = "compaction summaries"
UNSUPPORTED_UNKNOWN = "unknown content"

CONVERSATION_RECORDS = ("user", "assistant")
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
IDENTIFIER = re.compile(r'[^\s"<>]+')


@dataclass(frozen=True)
class ClaudeHistoryResult:
    """The outcome of one Claude transcript read.

    ``outcome`` is one of ``HISTORY_AVAILABLE``, ``HISTORY_MISSING`` or
    ``HISTORY_UNREACHABLE``. Only an available result carries events; an
    unreachable result is temporary and must not be treated as missing history.
    """

    session_id: str
    outcome: str
    events: tuple[ConversationEvent, ...] = ()
    unsupported: tuple[str, ...] = ()
    complete: bool = False
    detail: str | None = None
    transcript: Path | None = None

    def __post_init__(self) -> None:
        _validate_session_id(self.session_id)
        if self.outcome not in HISTORY_OUTCOMES:
            raise ValueError(f"Unsupported Claude history outcome: {self.outcome!r}")
        events = tuple(self.events)
        for event in events:
            if not isinstance(event, ConversationEvent):
                raise ValueError(
                    "Claude history events must be ConversationEvent objects"
                )
            if event.harness != HARNESS:
                raise ValueError(
                    "Claude history contains an event from another harness"
                )
        if self.outcome != HISTORY_AVAILABLE and events:
            raise ValueError("An unavailable Claude history cannot return events")
        if not isinstance(self.complete, bool):
            raise ValueError("complete must be a boolean")
        if self.outcome != HISTORY_AVAILABLE and self.complete:
            raise ValueError("An unavailable Claude history cannot be complete")
        if self.detail is not None and not isinstance(self.detail, str):
            raise ValueError("detail must be a string or None")
        if self.transcript is not None and not isinstance(self.transcript, Path):
            raise ValueError("transcript must be a Path")
        categories = tuple(dict.fromkeys(self.unsupported))
        for category in categories:
            if not isinstance(category, str) or not category.strip():
                raise ValueError("Unsupported categories must be non-empty names")
        object.__setattr__(self, "events", events)
        object.__setattr__(self, "unsupported", categories)

    @property
    def harness(self) -> str:
        return HARNESS

    @property
    def status(self) -> str:
        return self.outcome

    @property
    def available(self) -> bool:
        return self.outcome == HISTORY_AVAILABLE

    @property
    def missing(self) -> bool:
        return self.outcome == HISTORY_MISSING

    @property
    def unreachable(self) -> bool:
        return self.outcome == HISTORY_UNREACHABLE

    @property
    def history_complete(self) -> bool:
        return self.complete

    @property
    def history(self) -> HarnessHistory | None:
        if not self.available:
            return None
        return HarnessHistory(HARNESS, self.events, self.unsupported)

    @property
    def error(self) -> str:
        return self.detail or ""


@dataclass(frozen=True)
class _EventDraft:
    role: str
    text: str
    tool_name: str | None = None


def claude_projects_dir(env: Mapping[str, str] | None = None) -> Path:
    """Return the Claude projects directory, respecting ``CLAUDE_CONFIG_DIR``."""
    values = os.environ if env is None else env
    configured = values.get("CLAUDE_CONFIG_DIR")
    config = Path(configured).expanduser() if configured else (Path.home() / ".claude")
    return config / "projects"


def read_claude_history(
    session_id: str,
    *,
    env: Mapping[str, str] | None = None,
    projects_dir: Path | str | None = None,
    ordinal_start: int = 0,
) -> ClaudeHistoryResult:
    """Read one Claude session transcript without modifying it.

    The transcript is located under ``projects_dir`` (or the configured Claude
    projects directory) exactly like the deletion path in ``session_groups``.
    """
    session = _validate_session_id(session_id)
    if (
        isinstance(ordinal_start, bool)
        or not isinstance(ordinal_start, int)
        or ordinal_start < 0
    ):
        raise ValueError("ordinal_start must be a non-negative integer")
    base = (
        Path(projects_dir).expanduser()
        if projects_dir is not None
        else claude_projects_dir(env)
    )
    try:
        matches, listing_error = _transcript_matches(base, session)
    except FileNotFoundError:
        return _missing(session)
    except NotADirectoryError:
        return _missing(session)
    except OSError as error:
        return _unreachable(
            session, "Could not list the Claude projects directory", error
        )
    if listing_error is not None and not matches:
        return _unreachable(
            session, "Could not list a Claude project directory", listing_error
        )
    if not matches:
        return _missing(session)
    return _read_transcript(matches[0], session, ordinal_start)


def _transcript_matches(base: Path, session: str) -> tuple[list[Path], OSError | None]:
    matches: list[Path] = []
    listing_error: OSError | None = None
    filename = f"{session}.jsonl"
    with os.scandir(base) as projects:
        for project in projects:
            try:
                if not project.is_dir():
                    continue
            except OSError as error:
                if listing_error is None:
                    listing_error = error
                continue
            try:
                with os.scandir(project.path) as files:
                    for entry in files:
                        if entry.name != filename:
                            continue
                        try:
                            if entry.is_file():
                                matches.append(Path(entry.path))
                        except OSError as error:
                            if listing_error is None:
                                listing_error = error
            except (FileNotFoundError, NotADirectoryError):
                continue
            except OSError as error:
                if listing_error is None:
                    listing_error = error
    matches.sort(key=str)
    return matches, listing_error


def _validate_session_id(value: Any) -> str:
    if not isinstance(value, str) or SESSION_ID.fullmatch(value) is None:
        raise ValueError("Invalid Claude session identifier")
    return value


def _missing(session_id: str) -> ClaudeHistoryResult:
    return ClaudeHistoryResult(
        session_id,
        HISTORY_MISSING,
        detail="Claude transcript for this session was not found",
    )


def _unreachable(
    session_id: str, detail: str, error: BaseException | None = None
) -> ClaudeHistoryResult:
    message = detail if error is None else f"{detail}: {error}"
    return ClaudeHistoryResult(
        session_id,
        HISTORY_UNREACHABLE,
        detail=message,
    )


def _read_transcript(
    path: Path, session_id: str, ordinal_start: int
) -> ClaudeHistoryResult:
    try:
        before = path.stat()
    except FileNotFoundError:
        return _missing(session_id)
    except OSError as error:
        return _unreachable(
            session_id, "Could not inspect the Claude transcript", error
        )
    if not stat.S_ISREG(before.st_mode):
        return _missing(session_id)

    events: list[ConversationEvent] = []
    unsupported: list[str] = []
    tool_names: dict[str, str] = {}
    complete = True
    ledger = TransferLedger()
    try:
        with path.open("rb") as handle:
            for position, raw in enumerate(handle):
                try:
                    line = raw.decode("utf-8")
                except UnicodeDecodeError:
                    complete = False
                    continue
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except (ValueError, RecursionError):
                    complete = False
                    continue
                if not isinstance(record, dict):
                    complete = False
                    continue
                drafts, malformed = _record_drafts(record, tool_names, unsupported)
                if malformed:
                    complete = False
                if not drafts:
                    continue
                uuid = record.get("uuid")
                if not _is_identifier(uuid):
                    complete = False
                    continue
                for index, draft in enumerate(drafts):
                    event_id = uuid if index == 0 else f"{uuid}:{index}"
                    is_import, import_id = _import_of(draft, ledger)
                    events.append(
                        ConversationEvent(
                            HARNESS,
                            ordinal_start + len(events),
                            draft.role,
                            draft.text,
                            event_id=event_id,
                            position=position,
                            is_import=is_import,
                            import_id=import_id,
                            tool_name=draft.tool_name,
                        )
                    )
    except FileNotFoundError:
        return _missing(session_id)
    except OSError as error:
        return _unreachable(session_id, "Could not read the Claude transcript", error)

    try:
        after = path.stat()
    except OSError:
        complete = False
    else:
        if (
            after.st_size,
            after.st_mtime_ns,
            after.st_ino,
        ) != (
            before.st_size,
            before.st_mtime_ns,
            before.st_ino,
        ):
            complete = False
    return ClaudeHistoryResult(
        session_id,
        HISTORY_AVAILABLE,
        tuple(events),
        tuple(unsupported),
        complete,
        None,
        path,
    )


def _record_drafts(
    record: dict[str, Any],
    tool_names: dict[str, str],
    unsupported: list[str],
) -> tuple[list[_EventDraft], bool]:
    record_type = record.get("type")
    if not isinstance(record_type, str) or not record_type:
        return [], True
    if record_type == "attachment":
        _add_unsupported(unsupported, UNSUPPORTED_ATTACHMENTS)
        return [], False
    if record_type not in CONVERSATION_RECORDS:
        return [], False
    if record.get("isSidechain") is True:
        return [], False
    if record.get("isMeta") is True:
        return [], False
    if record.get("isCompactSummary") is True:
        _add_unsupported(unsupported, UNSUPPORTED_COMPACTION)
        return [], False

    message = record.get("message")
    if not isinstance(message, dict):
        return [], True
    role = message.get("role")
    if role not in CONVERSATION_RECORDS:
        role = record_type
    drafts: list[_EventDraft] = []
    content = message.get("content")
    if isinstance(content, str):
        if content:
            drafts.append(_EventDraft(role, content))
        return drafts, False
    if not isinstance(content, list):
        return drafts, True

    malformed = False
    for block in content:
        if isinstance(block, str):
            if block:
                drafts.append(_EventDraft(role, block))
            continue
        if not isinstance(block, dict):
            malformed = True
            continue
        kind = block.get("type")
        if not isinstance(kind, str) or not kind:
            malformed = True
            continue
        if kind == "text":
            text = block.get("text")
            if isinstance(text, str):
                if text:
                    drafts.append(_EventDraft(role, text))
            else:
                malformed = True
        elif kind in ("thinking", "redacted_thinking"):
            _add_unsupported(unsupported, UNSUPPORTED_HIDDEN_REASONING)
        elif kind == "tool_use":
            name = _tool_name(block.get("name"))
            tool_id = block.get("id")
            if _is_identifier(tool_id) and name is not None:
                tool_names[tool_id] = name
            drafts.append(
                _EventDraft(ROLE_TOOL_CALL, _tool_use_text(block.get("input")), name)
            )
        elif kind == "tool_result":
            tool_id = block.get("tool_use_id")
            name = tool_names.get(tool_id) if isinstance(tool_id, str) else None
            drafts.append(
                _EventDraft(
                    ROLE_TOOL_RESULT, _tool_result_text(block, unsupported), name
                )
            )
        elif kind == "image":
            _add_unsupported(unsupported, UNSUPPORTED_IMAGES)
        else:
            _add_unsupported(unsupported, _content_category(kind))
    return drafts, malformed


def _tool_use_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return "{}"
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    except (TypeError, ValueError):
        return str(value)


def _tool_result_text(block: dict[str, Any], unsupported: list[str]) -> str:
    value = block.get("content")
    if value is None and "text" in block:
        value = block.get("text")
    return _content_text(value, unsupported)


def _content_text(value: Any, unsupported: list[str]) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for part in value:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                kind = part.get("type")
                if kind == "text":
                    text = part.get("text")
                    if isinstance(text, str):
                        parts.append(text)
                    else:
                        _add_unsupported(unsupported, UNSUPPORTED_UNKNOWN)
                elif kind == "image":
                    _add_unsupported(unsupported, UNSUPPORTED_IMAGES)
                elif kind == "tool_result":
                    parts.append(_content_text(part.get("content"), unsupported))
                else:
                    _add_unsupported(unsupported, _content_category(kind))
            else:
                _add_unsupported(unsupported, UNSUPPORTED_UNKNOWN)
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        kind = value.get("type")
        if kind == "text":
            text = value.get("text")
            return text if isinstance(text, str) else ""
        if kind == "image":
            _add_unsupported(unsupported, UNSUPPORTED_IMAGES)
            return ""
        if "text" in value:
            text = value.get("text")
            return text if isinstance(text, str) else ""
        _add_unsupported(unsupported, _content_category(kind))
        return ""
    if value is None:
        return ""
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    except (TypeError, ValueError):
        return str(value)


def _import_of(draft: _EventDraft, ledger: TransferLedger) -> tuple[bool, str | None]:
    if draft.role != ROLE_USER or not draft.text:
        return False, None
    probe = ConversationEvent(
        HARNESS, 0, ROLE_USER, draft.text, event_id="claude-import-probe"
    )
    if not ledger.is_import_event(probe):
        return False, None
    markers = find_import_markers(draft.text)
    return True, markers[0].import_id if markers else None


def _tool_name(value: Any) -> str | None:
    return value if _is_identifier(value) else None


def _is_identifier(value: Any) -> bool:
    return isinstance(value, str) and IDENTIFIER.fullmatch(value) is not None


def _content_category(kind: Any) -> str:
    if kind == "image":
        return UNSUPPORTED_IMAGES
    if kind in ("thinking", "redacted_thinking"):
        return UNSUPPORTED_HIDDEN_REASONING
    if isinstance(kind, str) and kind:
        return f"content block: {kind}"
    return UNSUPPORTED_UNKNOWN


def _add_unsupported(values: list[str], category: str) -> None:
    if category not in values:
        values.append(category)

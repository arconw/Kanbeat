"""Read a Pi session file into normalized conversation events.

The module is the Pi-specific reader for harness conversation transfer. A
session file is opened only when ``read_pi_history`` is called. The file is
never written to, never mutated and its content is never logged.

Pi stores the conversation as a tree of entries. The reader follows ``parentId``
from the last appended entry to the root and reverses that chain, so only the
active branch is exported. Conversation messages are normalized; entry types
and content blocks that cannot be exported are reported through ``unsupported``
by category.
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
    ROLE_ASSISTANT,
    ROLE_TOOL_CALL,
    ROLE_TOOL_RESULT,
    ROLE_USER,
    ConversationEvent,
    HarnessHistory,
    import_id_for_text,
)

HARNESS = "pi"
HISTORY_AVAILABLE = "available"
HISTORY_MISSING = "missing"
HISTORY_UNREACHABLE = "unreachable"
HISTORY_OUTCOMES = (HISTORY_AVAILABLE, HISTORY_MISSING, HISTORY_UNREACHABLE)

UNSUPPORTED_HIDDEN_REASONING = "hidden reasoning"
UNSUPPORTED_IMAGES = "images"
UNSUPPORTED_COMPACTION = "compaction summaries"
UNSUPPORTED_BRANCH_SUMMARIES = "branch summaries"
UNSUPPORTED_CONTEXT_EDITS = "context edits"
UNSUPPORTED_CUSTOM_MESSAGES = "custom messages"
UNSUPPORTED_TRUNCATED_OUTPUT = "truncated bash output"
UNSUPPORTED_UNKNOWN = "unknown content"

BOOKKEEPING_ENTRY_TYPES = frozenset(
    (
        "custom",
        "label",
        "model_change",
        "session_info",
        "thinking_level_change",
        "usage",
    )
)

ROLE_SYSTEM = "system"
ROLE_TOOL_RESULT_MESSAGE = "toolResult"
ROLE_BASH_EXECUTION = "bashExecution"
ROLE_CUSTOM = "custom"
ROLE_BRANCH_SUMMARY = "branchSummary"
ROLE_COMPACTION_SUMMARY = "compactionSummary"

SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
IDENTIFIER = re.compile(r'[^\s"<>]+')


@dataclass(frozen=True)
class PiHistoryResult:
    """The outcome of one Pi session read.

    ``outcome`` is one of ``HISTORY_AVAILABLE``, ``HISTORY_MISSING`` or
    ``HISTORY_UNREACHABLE``. Only an available result carries events. An
    unreachable result is temporary and must not be treated as missing history;
    a missing result means the session file no longer exists.
    """

    session_id: str
    outcome: str
    events: tuple[ConversationEvent, ...] = ()
    unsupported: tuple[str, ...] = ()
    complete: bool = False
    detail: str | None = None
    session_file: Path | None = None

    def __post_init__(self) -> None:
        _validate_session_id(self.session_id)
        if self.outcome not in HISTORY_OUTCOMES:
            raise ValueError(f"Unsupported Pi history outcome: {self.outcome!r}")
        events = tuple(self.events)
        for event in events:
            if not isinstance(event, ConversationEvent):
                raise ValueError("Pi history events must be ConversationEvent objects")
            if event.harness != HARNESS:
                raise ValueError("Pi history contains an event from another harness")
        if self.outcome != HISTORY_AVAILABLE and events:
            raise ValueError("An unavailable Pi history cannot return events")
        if not isinstance(self.complete, bool):
            raise ValueError("complete must be a boolean")
        if self.outcome != HISTORY_AVAILABLE and self.complete:
            raise ValueError("An unavailable Pi history cannot be complete")
        if self.detail is not None and not isinstance(self.detail, str):
            raise ValueError("detail must be a string or None")
        if self.session_file is not None and not isinstance(self.session_file, Path):
            raise ValueError("session_file must be a Path")
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


def pi_sessions_dir(env: Mapping[str, str] | None = None) -> Path:
    """Return the Pi session directory, respecting ``PI_CODING_AGENT_SESSION_DIR``."""
    values = os.environ if env is None else env
    configured = values.get("PI_CODING_AGENT_SESSION_DIR")
    base = (
        Path(configured).expanduser()
        if configured
        else (Path.home() / ".pi" / "agent" / "sessions")
    )
    return base


def read_pi_history(
    session_id: str,
    *,
    env: Mapping[str, str] | None = None,
    sessions_dir: Path | str | None = None,
    ordinal_start: int = 0,
) -> PiHistoryResult:
    """Read one Pi session file without modifying it.

    The file is located under ``sessions_dir`` (or the configured Pi session
    directory) exactly like the deletion path in ``session_groups``.
    """
    session = _validate_session_id(session_id)
    if (
        isinstance(ordinal_start, bool)
        or not isinstance(ordinal_start, int)
        or ordinal_start < 0
    ):
        raise ValueError("ordinal_start must be a non-negative integer")
    base = (
        Path(sessions_dir).expanduser()
        if sessions_dir is not None
        else pi_sessions_dir(env)
    )
    try:
        matches, listing_error = _session_matches(base, session)
    except (FileNotFoundError, NotADirectoryError):
        return _missing(session)
    except OSError as error:
        return _unreachable(session, "Could not list the Pi session directory", error)
    if listing_error is not None and not matches:
        return _unreachable(
            session, "Could not list a Pi session directory", listing_error
        )
    if not matches:
        return _missing(session)
    return _read_session_file(matches[0], session, ordinal_start)


def _session_matches(base: Path, session: str) -> tuple[list[Path], OSError | None]:
    matches: list[Path] = []
    listing_error: OSError | None = None
    suffix = f"_{session}.jsonl"
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
                        if not entry.name.endswith(suffix):
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
        raise ValueError("Invalid Pi session identifier")
    return value


def _missing(session_id: str) -> PiHistoryResult:
    return PiHistoryResult(
        session_id,
        HISTORY_MISSING,
        detail="Pi session file for this session was not found",
    )


def _unreachable(
    session_id: str, detail: str, error: BaseException | None = None
) -> PiHistoryResult:
    message = detail if error is None else f"{detail}: {error}"
    return PiHistoryResult(
        session_id,
        HISTORY_UNREACHABLE,
        detail=message,
    )


def _read_session_file(
    path: Path, session_id: str, ordinal_start: int
) -> PiHistoryResult:
    try:
        before = path.stat()
    except FileNotFoundError:
        return _missing(session_id)
    except OSError as error:
        return _unreachable(session_id, "Could not inspect the Pi session file", error)
    if not stat.S_ISREG(before.st_mode):
        return _missing(session_id)

    header: dict[str, Any] | None = None
    records: list[tuple[int, dict[str, Any]]] = []
    complete = True
    expect_header = True
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
                if record.get("type") == "session":
                    if header is None:
                        header = record
                        if not expect_header:
                            complete = False
                    else:
                        complete = False
                    expect_header = False
                    continue
                if expect_header:
                    complete = False
                    expect_header = False
                records.append((position, record))
    except FileNotFoundError:
        return _missing(session_id)
    except OSError as error:
        return _unreachable(session_id, "Could not read the Pi session file", error)

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

    if header is None:
        complete = False
    elif header.get("id") != session_id:
        complete = False

    entries, positions, complete = _entry_index(records, complete)
    chain, complete = _active_branch(entries, complete)
    events, unsupported, mapping_complete = _chain_events(
        chain, positions, ordinal_start
    )
    return PiHistoryResult(
        session_id,
        HISTORY_AVAILABLE,
        tuple(events),
        tuple(unsupported),
        complete and mapping_complete,
        None,
        path,
    )


def _entry_index(
    records: list[tuple[int, dict[str, Any]]], complete: bool
) -> tuple[dict[str, dict[str, Any]], dict[str, int], bool]:
    entries: dict[str, dict[str, Any]] = {}
    positions: dict[str, int] = {}
    for position, record in records:
        entry_id = record.get("id")
        if not _is_identifier(entry_id):
            complete = False
            continue
        if entry_id in entries:
            complete = False
            continue
        if "parentId" not in record:
            complete = False
        parent = record.get("parentId")
        if parent is not None and not _is_identifier(parent):
            complete = False
        entries[entry_id] = record
        positions[entry_id] = position
    return entries, positions, complete


def _active_branch(
    entries: dict[str, dict[str, Any]],
    complete: bool,
) -> tuple[list[dict[str, Any]], bool]:
    if not entries:
        return [], complete
    current: str | None = list(entries)[-1]
    branch: list[dict[str, Any]] = []
    seen: set[str] = set()
    while current is not None:
        if current in seen:
            complete = False
            break
        seen.add(current)
        entry = entries.get(current)
        if entry is None:
            complete = False
            break
        branch.append(entry)
        parent = entry.get("parentId")
        if parent is not None and not _is_identifier(parent):
            complete = False
            break
        current = parent
    branch.reverse()
    return branch, complete


def _chain_events(
    chain: list[dict[str, Any]],
    positions: dict[str, int],
    ordinal_start: int,
) -> tuple[list[ConversationEvent], list[str], bool]:
    events: list[ConversationEvent] = []
    unsupported: list[str] = []
    complete = True
    for entry in chain:
        entry_id = entry["id"]
        drafts, malformed = _entry_drafts(entry, unsupported)
        if malformed:
            complete = False
        import_id = _message_import_id(drafts)
        for index, draft in enumerate(drafts):
            event_id = entry_id if index == 0 else f"{entry_id}:{index}"
            events.append(
                ConversationEvent(
                    HARNESS,
                    ordinal_start + len(events),
                    draft.role,
                    draft.text,
                    event_id=event_id,
                    position=positions[entry_id],
                    is_import=import_id is not None and draft.role == ROLE_USER,
                    import_id=import_id if draft.role == ROLE_USER else None,
                    tool_name=draft.tool_name,
                )
            )
    return events, unsupported, complete


def _entry_drafts(
    entry: dict[str, Any], unsupported: list[str]
) -> tuple[list[_EventDraft], bool]:
    entry_type = entry.get("type")
    if not isinstance(entry_type, str) or not entry_type:
        return [], True
    if entry_type == "message":
        return _message_drafts(entry.get("message"), unsupported)
    if entry_type == "compaction":
        _add_unsupported(unsupported, UNSUPPORTED_COMPACTION)
        return [], False
    if entry_type == "branch_summary":
        _add_unsupported(unsupported, UNSUPPORTED_BRANCH_SUMMARIES)
        return [], False
    if entry_type == "context_edit":
        _add_unsupported(unsupported, UNSUPPORTED_CONTEXT_EDITS)
        return [], False
    if entry_type == "custom_message":
        _add_unsupported(unsupported, UNSUPPORTED_CUSTOM_MESSAGES)
        return [], False
    if entry_type in BOOKKEEPING_ENTRY_TYPES:
        return [], False
    _add_unsupported(unsupported, UNSUPPORTED_UNKNOWN)
    return [], False


def _message_drafts(
    message: Any, unsupported: list[str]
) -> tuple[list[_EventDraft], bool]:
    if not isinstance(message, dict):
        return [], True
    role = message.get("role")
    if role == ROLE_SYSTEM:
        return [], False
    if role == ROLE_USER:
        return _user_drafts(message.get("content"), unsupported)
    if role == ROLE_ASSISTANT:
        return _assistant_drafts(message.get("content"), unsupported)
    if role == ROLE_TOOL_RESULT_MESSAGE:
        tool_name = _tool_name(message.get("toolName"))
        text, malformed = _content_text(message.get("content"), unsupported)
        if not text:
            return [], malformed
        return [_EventDraft(ROLE_TOOL_RESULT, text, tool_name)], malformed
    if role == ROLE_BASH_EXECUTION:
        if message.get("truncated") is True:
            _add_unsupported(unsupported, UNSUPPORTED_TRUNCATED_OUTPUT)
        text = _bash_text(message)
        if not text:
            return [], False
        return [_EventDraft(ROLE_TOOL_CALL, text, "bash")], False
    if role == ROLE_CUSTOM:
        _add_unsupported(unsupported, UNSUPPORTED_CUSTOM_MESSAGES)
        return [], False
    if role == ROLE_BRANCH_SUMMARY:
        _add_unsupported(unsupported, UNSUPPORTED_BRANCH_SUMMARIES)
        return [], False
    if role == ROLE_COMPACTION_SUMMARY:
        _add_unsupported(unsupported, UNSUPPORTED_COMPACTION)
        return [], False
    if isinstance(role, str) and role:
        _add_unsupported(unsupported, f"message role {role}")
        return [], False
    return [], True


def _user_drafts(
    content: Any, unsupported: list[str]
) -> tuple[list[_EventDraft], bool]:
    if isinstance(content, str):
        return ([_EventDraft(ROLE_USER, content)] if content else []), False
    if not isinstance(content, list):
        return [], True
    drafts: list[_EventDraft] = []
    malformed = False
    for block in content:
        if isinstance(block, str):
            if block:
                drafts.append(_EventDraft(ROLE_USER, block))
            continue
        if not isinstance(block, dict):
            malformed = True
            continue
        kind = block.get("type")
        if kind == "text":
            text = block.get("text")
            if isinstance(text, str):
                if text:
                    drafts.append(_EventDraft(ROLE_USER, text))
            else:
                malformed = True
        elif kind == "image":
            _add_unsupported(unsupported, UNSUPPORTED_IMAGES)
        else:
            _add_unsupported(unsupported, _content_category(kind))
    return drafts, malformed


def _assistant_drafts(
    content: Any, unsupported: list[str]
) -> tuple[list[_EventDraft], bool]:
    if isinstance(content, str):
        return ([_EventDraft(ROLE_ASSISTANT, content)] if content else []), False
    if not isinstance(content, list):
        return [], True
    drafts: list[_EventDraft] = []
    malformed = False
    for block in content:
        if isinstance(block, str):
            if block:
                drafts.append(_EventDraft(ROLE_ASSISTANT, block))
            continue
        if not isinstance(block, dict):
            malformed = True
            continue
        kind = block.get("type")
        if kind == "text":
            text = block.get("text")
            if isinstance(text, str):
                if text:
                    drafts.append(_EventDraft(ROLE_ASSISTANT, text))
            else:
                malformed = True
        elif kind == "thinking":
            _add_unsupported(unsupported, UNSUPPORTED_HIDDEN_REASONING)
        elif kind == "toolCall":
            drafts.append(
                _EventDraft(
                    ROLE_TOOL_CALL,
                    _tool_call_text(block.get("arguments")),
                    _tool_name(block.get("name")),
                )
            )
        elif kind == "image":
            _add_unsupported(unsupported, UNSUPPORTED_IMAGES)
        else:
            _add_unsupported(unsupported, _content_category(kind))
    return drafts, malformed


def _content_text(value: Any, unsupported: list[str]) -> tuple[str, bool]:
    if isinstance(value, str):
        return value, False
    if not isinstance(value, list):
        return "", True
    parts: list[str] = []
    malformed = False
    for block in value:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            malformed = True
            continue
        kind = block.get("type")
        if kind == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
            else:
                malformed = True
        elif kind == "image":
            _add_unsupported(unsupported, UNSUPPORTED_IMAGES)
        else:
            _add_unsupported(unsupported, _content_category(kind))
    return "\n".join(part for part in parts if part), malformed


def _bash_text(message: dict[str, Any]) -> str:
    command = _as_text(message.get("command"))
    output = _as_text(message.get("output"))
    if command and output:
        return f"{command}\n\nOutput:\n{output}"
    return command or output


def _tool_call_text(value: Any) -> str:
    if value is None:
        return "{}"
    if isinstance(value, str):
        return value
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


def _message_import_id(drafts: list[_EventDraft]) -> str | None:
    texts = [draft.text for draft in drafts if draft.role == ROLE_USER and draft.text]
    if not texts:
        return None
    return import_id_for_text("\n".join(texts))


def _content_category(kind: Any) -> str:
    if isinstance(kind, str) and kind:
        return f"content block: {kind}"
    return UNSUPPORTED_UNKNOWN


def _tool_name(value: Any) -> str | None:
    return value if _is_identifier(value) else None


def _is_identifier(value: Any) -> bool:
    return isinstance(value, str) and IDENTIFIER.fullmatch(value) is not None


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _add_unsupported(values: list[str], category: str) -> None:
    if category not in values:
        values.append(category)

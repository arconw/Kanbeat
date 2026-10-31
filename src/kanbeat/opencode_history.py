"""Read OpenCode conversation history into normalized transfer events.

The reader uses only the shared ``opencode serve`` host. It never writes to a
session and never logs message content. Tool inputs and results become labelled
events; reasoning, attachments and harness bookkeeping are reported through
``unsupported`` instead of being copied into the conversation.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import json
import re
from typing import Any

from .opencode_host import (
    OpenCodeHostError,
    OpenCodeHttpError,
    OpenCodeServeHost,
    OpenCodeTimeout,
    validate_session_id,
)
from .session_transfer import (
    ConversationEvent,
    HarnessHistory,
    ROLE_ASSISTANT,
    ROLE_TOOL_CALL,
    ROLE_TOOL_RESULT,
    ROLE_USER,
)

HARNESS = "opencode"
HISTORY_AVAILABLE = "available"
HISTORY_MISSING = "missing"
HISTORY_UNREACHABLE = "unreachable"
HISTORY_OUTCOMES = (HISTORY_AVAILABLE, HISTORY_MISSING, HISTORY_UNREACHABLE)
MESSAGE_PAGE_SIZE = 50

UNSUPPORTED_ATTACHMENTS = "attachments"
UNSUPPORTED_IGNORED = "ignored"
UNSUPPORTED_SYNTHETIC = "synthetic"

_IMPORT_START = re.compile(
    r'^<!--\s*kanbeat-import\s+id="(?P<import_id>[^"\s<>]+)"\s+'
    r'source="[^"\s<>]+"\s+events="\d+-\d+"\s*-->'
)
_IMPORT_END = re.compile(
    r'<!--\s*/kanbeat-import\s+id="(?P<import_id>[^"\s<>]+)"\s*-->'
)
_BOOKKEEPING_PARTS = frozenset(
    (
        "agent",
        "compaction",
        "patch",
        "retry",
        "snapshot",
        "step-finish",
        "step-start",
        "subtask",
    )
)


@dataclass(frozen=True)
class MappedOpenCodeHistory:
    events: tuple[ConversationEvent, ...]
    unsupported: tuple[str, ...]

    def __post_init__(self) -> None:
        events = tuple(self.events)
        for event in events:
            if not isinstance(event, ConversationEvent):
                raise ValueError("Mapped history needs ConversationEvent objects")
            if event.harness != HARNESS:
                raise ValueError("Mapped history contains another harness")
        categories = tuple(self.unsupported)
        for category in categories:
            if not isinstance(category, str) or not category.strip():
                raise ValueError("Unsupported categories must be non-empty names")
        object.__setattr__(self, "events", events)
        object.__setattr__(self, "unsupported", tuple(dict.fromkeys(categories)))

    def history(self) -> HarnessHistory:
        return HarnessHistory(HARNESS, self.events, self.unsupported)


@dataclass(frozen=True)
class OpenCodeHistoryResult:
    session_id: str
    outcome: str
    events: tuple[ConversationEvent, ...] = ()
    unsupported: tuple[str, ...] = ()
    complete: bool = False
    detail: str | None = None

    def __post_init__(self) -> None:
        validate_session_id(self.session_id)
        if self.outcome not in HISTORY_OUTCOMES:
            raise ValueError(f"Unsupported history outcome: {self.outcome!r}")
        if not isinstance(self.complete, bool):
            raise ValueError("complete must be a boolean")
        if self.detail is not None and not isinstance(self.detail, str):
            raise ValueError("detail must be a string")
        events = tuple(self.events)
        for event in events:
            if not isinstance(event, ConversationEvent):
                raise ValueError("History events must be ConversationEvent objects")
            if event.harness != HARNESS:
                raise ValueError("History contains another harness")
        if self.outcome != HISTORY_AVAILABLE and events:
            raise ValueError("A failed history read cannot return events")
        if self.outcome != HISTORY_AVAILABLE and self.complete:
            raise ValueError("A failed history read cannot be complete")
        categories = tuple(self.unsupported)
        for category in categories:
            if not isinstance(category, str) or not category.strip():
                raise ValueError("Unsupported categories must be non-empty names")
        object.__setattr__(self, "events", events)
        object.__setattr__(self, "unsupported", tuple(dict.fromkeys(categories)))

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
    def history(self) -> HarnessHistory | None:
        if not self.available:
            return None
        return HarnessHistory(HARNESS, self.events, self.unsupported)


def _identifier(value: Any) -> str | None:
    if (
        isinstance(value, str)
        and value
        and not any(char.isspace() or char in '"<>' for char in value)
    ):
        return value
    return None


def _import_ids(values: Iterable[str]) -> frozenset[str]:
    if isinstance(values, str):
        raise ValueError("Import message ids must be a collection")
    identifiers: set[str] = set()
    for value in values:
        identifier = _identifier(value)
        if identifier is None:
            raise ValueError("Import message ids must be stable identifiers")
        identifiers.add(identifier)
    return frozenset(identifiers)


def _import_id(text: str) -> str | None:
    stripped = text.strip()
    start = _IMPORT_START.match(stripped)
    if start is None:
        return None
    identifier = start.group("import_id")
    ends = [
        match
        for match in _IMPORT_END.finditer(stripped)
        if match.group("import_id") == identifier
    ]
    if not ends or ends[-1].end() != len(stripped):
        return None
    return identifier


def _tool_name(value: Any) -> str | None:
    return _identifier(value)


def _tool_call_text(state: Mapping[str, Any]) -> str:
    value = state.get("input")
    if isinstance(value, Mapping) and value:
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return ""
    title = state.get("title")
    if isinstance(title, str) and title.strip():
        return title
    return ""


def _tool_output_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return ""


def _tool_result_text(state: Mapping[str, Any]) -> str:
    metadata = state.get("metadata")
    if isinstance(metadata, Mapping) and metadata.get("interrupted") is True:
        interrupted = metadata.get("output")
        if isinstance(interrupted, str):
            return interrupted
    return _tool_output_text(state.get("error"))


def _part_event_id(part: Mapping[str, Any], suffix: str) -> str | None:
    identifier = _identifier(part.get("id"))
    return f"{identifier}:{suffix}" if identifier is not None else None


def _message_parts(message: Mapping[str, Any]) -> Sequence[Any]:
    parts = message.get("parts", ())
    if isinstance(parts, (str, bytes)) or not isinstance(parts, Sequence):
        raise ValueError("OpenCode message parts are malformed")
    return parts


def map_opencode_messages(
    messages: Iterable[Mapping[str, Any]],
    *,
    import_message_ids: Iterable[str] = (),
    ordinal_start: int = 0,
) -> MappedOpenCodeHistory:
    if (
        isinstance(ordinal_start, bool)
        or not isinstance(ordinal_start, int)
        or ordinal_start < 0
    ):
        raise ValueError("ordinal_start must be a non-negative integer")
    known_imports = _import_ids(import_message_ids)
    events: list[ConversationEvent] = []
    unsupported: list[str] = []
    position = 0

    def add(
        role: str,
        text: str,
        event_id: str | None,
        *,
        tool_name: str | None = None,
        is_import: bool = False,
        import_id: str | None = None,
    ) -> None:
        nonlocal position
        events.append(
            ConversationEvent(
                harness=HARNESS,
                ordinal=ordinal_start + position,
                role=role,
                text=text,
                event_id=event_id,
                position=position,
                is_import=is_import,
                import_id=import_id,
                tool_name=tool_name,
            )
        )
        position += 1

    for message in messages:
        if not isinstance(message, Mapping):
            raise ValueError("OpenCode message is malformed")
        info = message.get("info")
        if not isinstance(info, Mapping):
            raise ValueError("OpenCode message info is malformed")
        message_id = _identifier(info.get("id"))
        if message_id is None:
            raise ValueError("OpenCode message id is malformed")
        role = info.get("role")
        if role not in (ROLE_USER, ROLE_ASSISTANT):
            unsupported.append(f"message role {role}")
            continue

        message_id_used = False
        user_texts: list[str] = []
        for part in _message_parts(message):
            if not isinstance(part, Mapping):
                raise ValueError("OpenCode message part is malformed")
            part_type = part.get("type")

            if part_type == "text":
                if part.get("synthetic") is True:
                    unsupported.append(UNSUPPORTED_SYNTHETIC)
                    continue
                if part.get("ignored") is True:
                    unsupported.append(UNSUPPORTED_IGNORED)
                    continue
                text = part.get("text")
                if not isinstance(text, str):
                    raise ValueError("OpenCode text part is malformed")
                if role == ROLE_USER:
                    user_texts.append(text)
                    continue
                event_id = (
                    message_id if not message_id_used else _identifier(part.get("id"))
                )
                message_id_used = True
                add(ROLE_ASSISTANT, text, event_id)
                continue

            if part_type == "tool":
                if role != ROLE_ASSISTANT:
                    unsupported.append("tool")
                    continue
                state = part.get("state")
                if not isinstance(state, Mapping):
                    state = {}
                tool_name = _tool_name(part.get("tool"))
                call_id = (
                    message_id
                    if not message_id_used
                    else _part_event_id(part, "call")
                )
                message_id_used = True
                add(
                    ROLE_TOOL_CALL,
                    _tool_call_text(state),
                    call_id,
                    tool_name=tool_name,
                )
                attachments = state.get("attachments")
                if isinstance(attachments, list) and attachments:
                    unsupported.append(UNSUPPORTED_ATTACHMENTS)
                status = state.get("status")
                if status == "completed":
                    add(
                        ROLE_TOOL_RESULT,
                        _tool_output_text(state.get("output")),
                        _part_event_id(part, "result"),
                        tool_name=tool_name,
                    )
                elif status == "error":
                    add(
                        ROLE_TOOL_RESULT,
                        _tool_result_text(state),
                        _part_event_id(part, "result"),
                        tool_name=tool_name,
                    )
                continue

            if part_type == "reasoning":
                unsupported.append("reasoning")
                continue
            if part_type == "file":
                unsupported.append("file")
                continue
            if isinstance(part_type, str) and part_type in _BOOKKEEPING_PARTS:
                unsupported.append(part_type)
                continue
            unsupported.append(
                part_type if isinstance(part_type, str) and part_type else "unknown part"
            )

        if role == ROLE_USER and user_texts:
            text = "\n".join(user_texts)
            import_id = _import_id(text)
            add(
                ROLE_USER,
                text,
                message_id if not message_id_used else None,
                is_import=import_id is not None or message_id in known_imports,
                import_id=import_id,
            )

    return MappedOpenCodeHistory(
        tuple(events), tuple(sorted(dict.fromkeys(unsupported)))
    )


def _failure_detail(error: OpenCodeHostError) -> str:
    if isinstance(error, OpenCodeTimeout):
        return "timeout"
    if isinstance(error, OpenCodeHttpError):
        return f"http_{error.status}"
    if "too large" in str(error):
        return "response_too_large"
    if "malformed" in str(error) or "not JSON" in str(error):
        return "malformed_response"
    return "unreachable"


async def read_opencode_history(
    host: OpenCodeServeHost,
    session_id: str,
    *,
    import_message_ids: Iterable[str] = (),
    page_size: int = MESSAGE_PAGE_SIZE,
    ordinal_start: int = 0,
) -> OpenCodeHistoryResult:
    session_id = validate_session_id(session_id)
    if isinstance(page_size, bool) or not isinstance(page_size, int) or page_size < 1:
        raise ValueError("page_size must be a positive integer")
    if (
        isinstance(ordinal_start, bool)
        or not isinstance(ordinal_start, int)
        or ordinal_start < 0
    ):
        raise ValueError("ordinal_start must be a non-negative integer")
    known_imports = _import_ids(import_message_ids)

    pages: list[tuple[Any, ...]] = []
    seen_cursors: set[str] = set()
    before: str | None = None
    while True:
        try:
            page = await host.read_messages_page(
                session_id, limit=page_size, before=before
            )
        except OpenCodeHttpError as error:
            if error.status == 404:
                return OpenCodeHistoryResult(
                    session_id,
                    HISTORY_MISSING,
                    detail="not_found",
                )
            return OpenCodeHistoryResult(
                session_id,
                HISTORY_UNREACHABLE,
                detail=_failure_detail(error),
            )
        except OpenCodeHostError as error:
            return OpenCodeHistoryResult(
                session_id,
                HISTORY_UNREACHABLE,
                detail=_failure_detail(error),
            )

        pages.append(tuple(page.messages))
        if page.next_cursor is None:
            break
        if page.next_cursor in seen_cursors:
            return OpenCodeHistoryResult(
                session_id,
                HISTORY_UNREACHABLE,
                detail="paging_loop",
            )
        seen_cursors.add(page.next_cursor)
        before = page.next_cursor

    ordered = tuple(
        message for page_messages in reversed(pages) for message in page_messages
    )
    try:
        mapped = map_opencode_messages(
            ordered,
            import_message_ids=known_imports,
            ordinal_start=ordinal_start,
        )
    except (TypeError, ValueError):
        return OpenCodeHistoryResult(
            session_id,
            HISTORY_UNREACHABLE,
            detail="malformed_history",
        )
    return OpenCodeHistoryResult(
        session_id,
        HISTORY_AVAILABLE,
        events=mapped.events,
        unsupported=mapped.unsupported,
        complete=True,
    )
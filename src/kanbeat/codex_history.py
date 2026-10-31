"""Read a Codex thread as normalized conversation events.

The reader pages ``thread/items/list`` through the shared app-server host and
maps each supported item to one :class:`~kanbeat.session_transfer.ConversationEvent`.
Item ids are the event ids. Reading never resumes, injects or otherwise mutates
the thread, and no conversation content is logged.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any

from .codex_host import (
    CodexHostError,
    CodexRpcError,
    thread_missing,
    validate_thread_id,
)
from .session_transfer import (
    ROLE_ASSISTANT,
    ROLE_TOOL_CALL,
    ROLE_TOOL_RESULT,
    ROLE_USER,
    ConversationEvent,
    HarnessHistory,
    import_id_for_text,
)

CODEX = "codex"
HISTORY_AVAILABLE = "available"
HISTORY_MISSING = "missing"
HISTORY_UNREACHABLE = "unreachable"
HISTORY_OUTCOMES = (HISTORY_AVAILABLE, HISTORY_MISSING, HISTORY_UNREACHABLE)

DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 100
DEFAULT_MAX_PAGES = 1000

UNSUPPORTED_REASONING = "reasoning"
UNSUPPORTED_ATTACHMENTS = "attachments"
UNSUPPORTED_ENCRYPTED = "encrypted content"
UNSUPPORTED_SKILLS = "skills"
UNSUPPORTED_MENTIONS = "mentions"
UNSUPPORTED_HOOK_PROMPTS = "hook prompts"
UNSUPPORTED_SUBAGENT_ACTIVITY = "sub-agent activity"
UNSUPPORTED_REVIEW_MODE = "review mode"
UNSUPPORTED_COLLAB_CALLS = "collab calls"
UNSUPPORTED_SLEEP = "sleep"
UNSUPPORTED_CONTEXT_COMPACTION = "context compaction"
UNSUPPORTED_WEB_RESULTS = "web search results"

MISSING_MESSAGE = "Codex thread no longer exists"
UNREACHABLE_MESSAGE = "Codex history is temporarily unreachable"
INVALID_PAGE_MESSAGE = "Codex returned an invalid history page"
INVALID_ITEM_MESSAGE = "Codex returned an invalid history item"
PAGE_LIMIT_MESSAGE = "Codex history exceeded the page limit"

_IDENTIFIER = re.compile(r'[^\s"<>]+')


@dataclass(frozen=True)
class CodexHistoryResult:
    """The outcome of reading one Codex thread.

    ``outcome`` is one of ``available``, ``missing`` or ``unreachable``. A
    missing or unreachable result carries no events; callers must not treat it
    as either an empty conversation or a complete history.
    """

    outcome: str
    history: HarnessHistory
    complete: bool = False
    error: str | None = None

    def __post_init__(self) -> None:
        if self.outcome not in HISTORY_OUTCOMES:
            raise ValueError(f"Unsupported Codex history outcome: {self.outcome!r}")
        if not isinstance(self.history, HarnessHistory):
            raise ValueError("Codex history must be a HarnessHistory")
        if self.history.harness != CODEX:
            raise ValueError("Codex history must contain only codex events")
        if not isinstance(self.complete, bool):
            raise ValueError("complete must be a boolean")
        if self.error is not None and not isinstance(self.error, str):
            raise ValueError("error must be a string or None")
        if self.outcome != HISTORY_AVAILABLE and self.complete:
            raise ValueError("Only available history can be complete")
        if self.outcome != HISTORY_AVAILABLE and self.history.events:
            raise ValueError("Unavailable history must not contain events")

    @property
    def events(self) -> tuple[ConversationEvent, ...]:
        return self.history.events

    @property
    def unsupported(self) -> tuple[str, ...]:
        return self.history.unsupported

    @property
    def available(self) -> bool:
        return self.outcome == HISTORY_AVAILABLE

    @property
    def missing(self) -> bool:
        return self.outcome == HISTORY_MISSING

    @property
    def unreachable(self) -> bool:
        return self.outcome == HISTORY_UNREACHABLE


class CodexHistoryReader:
    """Page and normalize one Codex thread without changing it."""

    def __init__(
        self,
        host: Any,
        *,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
    ):
        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or not 1 <= page_size <= MAX_PAGE_SIZE
        ):
            raise ValueError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")
        if (
            isinstance(max_pages, bool)
            or not isinstance(max_pages, int)
            or max_pages < 1
        ):
            raise ValueError("max_pages must be a positive integer")
        self.host = host
        self.page_size = page_size
        self.max_pages = max_pages

    async def read(
        self, thread_id: str, *, ordinal_start: int = 0
    ) -> CodexHistoryResult:
        thread_id = validate_thread_id(thread_id)
        if (
            isinstance(ordinal_start, bool)
            or not isinstance(ordinal_start, int)
            or ordinal_start < 0
        ):
            raise ValueError("ordinal_start must be a non-negative integer")

        entries: list[dict[str, Any]] = []
        cursor: str | None = None
        complete = False
        error: str | None = None
        for _page in range(self.max_pages):
            params: dict[str, Any] = {
                "threadId": thread_id,
                "limit": self.page_size,
                "sortDirection": "asc",
            }
            if cursor is not None:
                params["cursor"] = cursor
            try:
                response = await self.host.request("thread/items/list", params)
            except CodexRpcError as exc:
                if thread_missing(exc):
                    return _failure(HISTORY_MISSING, MISSING_MESSAGE)
                return _failure(HISTORY_UNREACHABLE, _error_text(exc))
            except (CodexHostError, TimeoutError, OSError) as exc:
                return _failure(HISTORY_UNREACHABLE, _error_text(exc))

            page, next_cursor = _valid_page(response)
            if page is None:
                return _failure(HISTORY_UNREACHABLE, INVALID_PAGE_MESSAGE)
            entries.extend(page)
            if next_cursor is None:
                complete = True
                break
            cursor = next_cursor
        else:
            error = PAGE_LIMIT_MESSAGE

        try:
            events, unsupported = _normalize(entries, ordinal_start)
        except (TypeError, ValueError):
            return _failure(HISTORY_UNREACHABLE, INVALID_ITEM_MESSAGE)
        return CodexHistoryResult(
            outcome=HISTORY_AVAILABLE,
            history=HarnessHistory(CODEX, events, unsupported),
            complete=complete,
            error=error,
        )


async def read_codex_history(
    host: Any,
    thread_id: str,
    *,
    ordinal_start: int = 0,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> CodexHistoryResult:
    """Read one Codex thread through ``host`` and return normalized history."""
    reader = CodexHistoryReader(host, page_size=page_size, max_pages=max_pages)
    return await reader.read(thread_id, ordinal_start=ordinal_start)


def _failure(outcome: str, error: str) -> CodexHistoryResult:
    return CodexHistoryResult(
        outcome=outcome,
        history=HarnessHistory(CODEX),
        complete=False,
        error=error,
    )


def _error_text(error: BaseException) -> str:
    message = str(error)[:200]
    return f"{UNREACHABLE_MESSAGE}: {message}" if message else UNREACHABLE_MESSAGE


def _valid_page(
    value: Any,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    if not isinstance(value, dict):
        return None, None
    data = value.get("data")
    if not isinstance(data, list):
        return None, None
    rows = []
    for row in data:
        if not isinstance(row, dict):
            return None, None
        rows.append(row)
    cursor = value.get("nextCursor")
    if cursor == "":
        cursor = None
    if cursor is not None and not isinstance(cursor, str):
        return None, None
    return rows, cursor


def _normalize(
    entries: list[dict[str, Any]], ordinal_start: int
) -> tuple[tuple[ConversationEvent, ...], tuple[str, ...]]:
    events: list[ConversationEvent] = []
    unsupported: set[str] = set()
    ordinal = ordinal_start
    for entry in entries:
        item = entry.get("item")
        if not isinstance(item, dict):
            raise ValueError("Codex history entry has no item")
        event = _event(item, ordinal, unsupported)
        if event is None:
            continue
        events.append(event)
        ordinal += 1
    return tuple(events), tuple(sorted(unsupported))


def _event(
    item: dict[str, Any], ordinal: int, unsupported: set[str]
) -> ConversationEvent | None:
    kind = item.get("type")
    item_id = item.get("id")
    if not isinstance(kind, str) or not isinstance(item_id, str) or not item_id:
        raise ValueError("Codex history item has no type or id")

    if kind == "userMessage":
        text = _user_text(item.get("content"), unsupported)
        if not text:
            return None
        return ConversationEvent(
            CODEX,
            ordinal,
            ROLE_USER,
            text,
            event_id=item_id,
            import_id=_import_id(text, item.get("clientId")),
        )
    if kind == "agentMessage":
        text = _as_text(item.get("text"))
        if not text:
            return None
        return ConversationEvent(CODEX, ordinal, ROLE_ASSISTANT, text, event_id=item_id)
    if kind == "plan":
        text = _as_text(item.get("text"))
        if not text:
            return None
        return ConversationEvent(CODEX, ordinal, ROLE_ASSISTANT, text, event_id=item_id)
    if kind == "reasoning":
        unsupported.add(UNSUPPORTED_REASONING)
        return None
    if kind == "commandExecution":
        text = _command_text(item)
        if not text:
            return None
        return ConversationEvent(
            CODEX,
            ordinal,
            ROLE_TOOL_CALL,
            text,
            event_id=item_id,
            tool_name="command",
        )
    if kind == "fileChange":
        text = _file_change_text(item)
        if not text:
            return None
        return ConversationEvent(
            CODEX,
            ordinal,
            ROLE_TOOL_CALL,
            text,
            event_id=item_id,
            tool_name="fileChange",
        )
    if kind == "mcpToolCall":
        text = _mcp_text(item, unsupported)
        if not text:
            return None
        return ConversationEvent(
            CODEX,
            ordinal,
            ROLE_TOOL_CALL,
            text,
            event_id=item_id,
            tool_name=_tool_name(item.get("tool")),
        )
    if kind == "dynamicToolCall":
        text = _dynamic_text(item, unsupported)
        if not text:
            return None
        return ConversationEvent(
            CODEX,
            ordinal,
            ROLE_TOOL_CALL,
            text,
            event_id=item_id,
            tool_name=_tool_name(item.get("tool")),
        )
    if kind == "functionCallOutput":
        text = _output_text(item.get("output"), unsupported)
        if not text:
            return None
        return ConversationEvent(
            CODEX,
            ordinal,
            ROLE_TOOL_RESULT,
            text,
            event_id=item_id,
            tool_name=_tool_name(item.get("name")),
        )
    if kind == "webSearch":
        text = _as_text(item.get("query"))
        if not text:
            return None
        results = item.get("results")
        if isinstance(results, list) and results:
            unsupported.add(UNSUPPORTED_WEB_RESULTS)
        return ConversationEvent(
            CODEX,
            ordinal,
            ROLE_TOOL_CALL,
            text,
            event_id=item_id,
            tool_name="webSearch",
        )
    if kind in ("imageView", "imageGeneration"):
        unsupported.add(UNSUPPORTED_ATTACHMENTS)
        return None
    if kind == "hookPrompt":
        unsupported.add(UNSUPPORTED_HOOK_PROMPTS)
        return None
    if kind == "subAgentActivity":
        unsupported.add(UNSUPPORTED_SUBAGENT_ACTIVITY)
        return None
    if kind == "collabAgentToolCall":
        unsupported.add(UNSUPPORTED_COLLAB_CALLS)
        return None
    if kind == "sleep":
        unsupported.add(UNSUPPORTED_SLEEP)
        return None
    if kind in ("enteredReviewMode", "exitedReviewMode"):
        unsupported.add(UNSUPPORTED_REVIEW_MODE)
        return None
    if kind == "contextCompaction":
        unsupported.add(UNSUPPORTED_CONTEXT_COMPACTION)
        return None
    unsupported.add(kind)
    return None


def _user_text(content: Any, unsupported: set[str]) -> str:
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for value in content:
        if not isinstance(value, dict):
            unsupported.add(UNSUPPORTED_ATTACHMENTS)
            continue
        kind = value.get("type")
        if kind == "text":
            text = _as_text(value.get("text"))
            if text:
                parts.append(text)
        elif kind in ("image", "localImage", "audio", "localAudio"):
            unsupported.add(UNSUPPORTED_ATTACHMENTS)
        elif kind == "skill":
            unsupported.add(UNSUPPORTED_SKILLS)
        elif kind == "mention":
            unsupported.add(UNSUPPORTED_MENTIONS)
        else:
            unsupported.add(UNSUPPORTED_ATTACHMENTS)
    return "\n".join(parts)


def _command_text(item: dict[str, Any]) -> str:
    command = _as_text(item.get("command"))
    output = item.get("aggregatedOutput")
    if isinstance(output, str) and output:
        if command:
            return f"{command}\n\nOutput:\n{output}"
        return output
    status = _as_text(item.get("status"))
    if status in ("failed", "declined") and command:
        return f"{command}\n\nStatus: {status}"
    return command


def _file_change_text(item: dict[str, Any]) -> str:
    changes = item.get("changes")
    if not isinstance(changes, list):
        return ""
    parts: list[str] = []
    for change in changes:
        if not isinstance(change, dict):
            continue
        path = _as_text(change.get("path"))
        kind = _as_text(change.get("kind"))
        header = " ".join(part for part in (kind, path) if part)
        diff = _as_text(change.get("diff"))
        if header and diff:
            parts.append(f"{header}\n{diff}")
        elif header:
            parts.append(header)
        elif diff:
            parts.append(diff)
    return "\n\n".join(parts)


def _mcp_text(item: dict[str, Any], unsupported: set[str]) -> str:
    parts: list[str] = []
    server = _as_text(item.get("server"))
    if server:
        parts.append(f"Server: {server}")
    arguments = _json_text(item.get("arguments"))
    if arguments:
        parts.append(f"Arguments: {arguments}")
    result = item.get("result")
    if isinstance(result, dict):
        output = _output_text(result.get("content"), unsupported)
        if output:
            parts.append(f"Result:\n{output}")
        structured = result.get("structuredContent")
        if structured is not None:
            text = _json_text(structured)
            if text:
                parts.append(f"Structured result: {text}")
    error = item.get("error")
    if isinstance(error, dict):
        message = _as_text(error.get("message"))
        if message:
            parts.append(f"Error: {message}")
    return "\n\n".join(parts)


def _dynamic_text(item: dict[str, Any], unsupported: set[str]) -> str:
    parts: list[str] = []
    arguments = _json_text(item.get("arguments"))
    if arguments:
        parts.append(f"Arguments: {arguments}")
    content = _output_text(item.get("contentItems"), unsupported)
    if content:
        parts.append(f"Result:\n{content}")
    return "\n\n".join(parts)


def _output_text(value: Any, unsupported: set[str]) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for entry in value:
            if isinstance(entry, str):
                parts.append(entry)
                continue
            if not isinstance(entry, dict):
                unsupported.add(UNSUPPORTED_ATTACHMENTS)
                continue
            kind = entry.get("type")
            if kind in ("text", "input_text", "output_text"):
                text = _as_text(entry.get("text"))
                if text:
                    parts.append(text)
            elif kind in ("image", "input_image", "audio", "input_audio"):
                unsupported.add(UNSUPPORTED_ATTACHMENTS)
            elif kind == "encrypted_content":
                unsupported.add(UNSUPPORTED_ENCRYPTED)
            else:
                text = _as_text(entry.get("text"))
                if text:
                    parts.append(text)
                elif "content" in entry:
                    nested = _output_text(entry.get("content"), unsupported)
                    if nested:
                        parts.append(nested)
                else:
                    unsupported.add(UNSUPPORTED_ATTACHMENTS)
        return "\n".join(parts)
    if isinstance(value, dict):
        return _output_text(value.get("content", value.get("text")), unsupported)
    return ""


def _json_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return ""


def _import_id(text: str, client_id: Any) -> str | None:
    marker = import_id_for_text(text)
    if marker is not None:
        return marker
    if isinstance(client_id, str) and _IDENTIFIER.fullmatch(client_id):
        return client_id
    return None


def _tool_name(value: Any) -> str | None:
    if isinstance(value, str) and value and _IDENTIFIER.fullmatch(value):
        return value
    return None


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else ""
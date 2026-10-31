import pytest

from kanbeat.opencode_history import (
    HISTORY_AVAILABLE,
    HISTORY_MISSING,
    HISTORY_UNREACHABLE,
    map_opencode_messages,
    read_opencode_history,
)
from kanbeat.opencode_host import (
    OpenCodeHostError,
    OpenCodeHttpError,
    OpenCodeMessagePage,
    OpenCodeTimeout,
)
from kanbeat.session_transfer import render_short_prompt

SESSION = "ses_historyfixture"


def text_part(part_id, message_id, text, **kwargs):
    return {
        "id": part_id,
        "sessionID": SESSION,
        "messageID": message_id,
        "type": "text",
        "text": text,
        **kwargs,
    }


def message(message_id, role, parts):
    return {
        "info": {"id": message_id, "sessionID": SESSION, "role": role},
        "parts": parts,
    }


class FakeHost:
    def __init__(self, pages=None, error=None):
        self.pages = pages or {}
        self.error = error
        self.calls = []

    async def read_messages_page(self, session_id, *, limit=50, before=None):
        self.calls.append((session_id, limit, before))
        if self.error is not None:
            raise self.error
        messages, cursor = self.pages[before]
        return OpenCodeMessagePage(tuple(messages), cursor)


def test_maps_text_tools_imports_and_unsupported_parts():
    import_text = render_short_prompt(
        "Read the private context file and continue.",
        "import-one",
        ["codex"],
        0,
        1,
    )
    messages = [
        message(
            "msg_user",
            "user",
            [
                text_part("prt_user", "msg_user", "Hello"),
                {
                    "id": "prt_file",
                    "messageID": "msg_user",
                    "type": "file",
                    "mime": "image/png",
                    "filename": "fixture-diagram.png",
                    "url": "data:image/png;base64,AAAA",
                },
            ],
        ),
        message(
            "msg_assistant",
            "assistant",
            [
                {
                    "id": "prt_reasoning",
                    "messageID": "msg_assistant",
                    "type": "reasoning",
                    "text": "hidden",
                },
                text_part("prt_text", "msg_assistant", "Answer"),
                {
                    "id": "prt_tool",
                    "messageID": "msg_assistant",
                    "type": "tool",
                    "callID": "call-one",
                    "tool": "bash",
                    "state": {
                        "status": "completed",
                        "input": {"command": "pwd"},
                        "output": "/work",
                        "title": "Run pwd",
                        "metadata": {},
                        "time": {"start": 1, "end": 2},
                    },
                },
                {
                    "id": "prt_step",
                    "messageID": "msg_assistant",
                    "type": "step-start",
                },
            ],
        ),
        message(
            "msg_import", "user", [text_part("prt_import", "msg_import", import_text)]
        ),
        message(
            "msg_known_import",
            "user",
            [text_part("prt_known", "msg_known_import", "Imported without marker")],
        ),
        message(
            "msg_reply",
            "user",
            [text_part("prt_reply", "msg_reply", "A new question")],
        ),
    ]

    mapped = map_opencode_messages(messages, import_message_ids={"msg_known_import"})

    assert [event.role for event in mapped.events] == [
        "user",
        "assistant",
        "tool_call",
        "tool_result",
        "user",
        "user",
        "user",
    ]
    assert [event.event_id for event in mapped.events] == [
        "msg_user",
        "msg_assistant",
        "prt_tool:call",
        "prt_tool:result",
        "msg_import",
        "msg_known_import",
        "msg_reply",
    ]
    assert [event.ordinal for event in mapped.events] == list(range(7))
    assert mapped.events[2].tool_name == "bash"
    assert mapped.events[3].tool_name == "bash"
    assert mapped.events[0].text == "Hello\n[File: fixture-diagram.png]"
    assert "AAAA" not in mapped.events[0].text
    assert mapped.events[1].text == "Answer"
    assert mapped.events[2].text == '{"command": "pwd"}'
    assert mapped.events[3].text == "/work"
    assert mapped.events[4].is_import and mapped.events[4].import_id == "import-one"
    assert mapped.events[5].is_import and mapped.events[5].import_id is None
    assert not mapped.events[6].is_import
    assert set(mapped.unsupported) == {"file", "reasoning", "step-start"}

    again = map_opencode_messages(messages, import_message_ids={"msg_known_import"})
    assert [event.key for event in again.events] == [
        event.key for event in mapped.events
    ]


def test_skips_synthetic_and_ignored_text_and_reports_attachments():
    messages = [
        message(
            "msg_user",
            "user",
            [
                text_part("prt_synthetic", "msg_user", "internal", synthetic=True),
                text_part("prt_ignored", "msg_user", "old", ignored=True),
                text_part("prt_user", "msg_user", "Visible"),
            ],
        ),
        message(
            "msg_assistant",
            "assistant",
            [
                {
                    "id": "prt_tool",
                    "messageID": "msg_assistant",
                    "type": "tool",
                    "callID": "call-two",
                    "tool": "read",
                    "state": {
                        "status": "completed",
                        "input": {"path": "notes.txt"},
                        "output": "notes",
                        "title": "Read notes",
                        "attachments": [
                            {
                                "id": "prt_attachment",
                                "messageID": "msg_assistant",
                                "type": "file",
                                "mime": "image/png",
                                "filename": "notes-preview.png",
                                "url": "data:image/png;base64,AAAA",
                            }
                        ],
                        "metadata": {},
                        "time": {"start": 1, "end": 2},
                    },
                }
            ],
        ),
    ]

    mapped = map_opencode_messages(messages)

    assert [event.text for event in mapped.events] == [
        "Visible",
        '{"path": "notes.txt"}',
        "notes\n[File: notes-preview.png]",
    ]
    assert "AAAA" not in "\n".join(event.text for event in mapped.events)
    assert set(mapped.unsupported) == {"attachments", "ignored", "synthetic"}


@pytest.mark.asyncio
async def test_reader_pages_backwards_and_returns_chronological_events():
    newer = [message("msg_new", "user", [text_part("prt_new", "msg_new", "Newest")])]
    older = [
        message("msg_old", "user", [text_part("prt_old", "msg_old", "Oldest")]),
        message(
            "msg_middle",
            "assistant",
            [text_part("prt_middle", "msg_middle", "Middle")],
        ),
    ]
    host = FakeHost({None: (newer, "cursor-one"), "cursor-one": (older, None)})

    result = await read_opencode_history(host, SESSION, page_size=2)

    assert result.outcome == HISTORY_AVAILABLE
    assert result.complete
    assert [event.text for event in result.events] == ["Oldest", "Middle", "Newest"]
    assert [call[2] for call in host.calls] == [None, "cursor-one"]
    assert all(call[1] == 2 for call in host.calls)


@pytest.mark.asyncio
async def test_oversized_page_retries_smaller_limit_on_the_same_cursor():
    class SizeLimitedHost:
        def __init__(self):
            self.calls = []

        async def read_messages_page(self, session_id, *, limit, before):
            self.calls.append((limit, before))
            if limit > 1:
                raise OpenCodeHostError("OpenCode response is too large")
            if before is None:
                return OpenCodeMessagePage(
                    (
                        message(
                            "msg_new",
                            "assistant",
                            [text_part("prt_new", "msg_new", "Newest")],
                        ),
                    ),
                    "older",
                )
            return OpenCodeMessagePage(
                (
                    message(
                        "msg_old",
                        "user",
                        [text_part("prt_old", "msg_old", "Oldest")],
                    ),
                )
            )

    host = SizeLimitedHost()

    result = await read_opencode_history(host, SESSION)

    assert result.available and result.complete
    assert [event.event_id for event in result.events] == ["msg_old", "msg_new"]
    assert host.calls[-2:] == [(1, None), (1, "older")]


@pytest.mark.asyncio
async def test_oversized_older_page_retries_same_cursor_without_losing_messages():
    class SizeLimitedOlderPageHost:
        def __init__(self):
            self.calls = []

        async def read_messages_page(self, session_id, *, limit, before):
            self.calls.append((limit, before))
            if before is None:
                return OpenCodeMessagePage(
                    (
                        message(
                            "msg_new",
                            "assistant",
                            [text_part("prt_new", "msg_new", "Newest")],
                        ),
                    ),
                    "older",
                )
            if limit > 1:
                raise OpenCodeHostError("OpenCode response is too large")
            return OpenCodeMessagePage(
                (
                    message(
                        "msg_old",
                        "user",
                        [text_part("prt_old", "msg_old", "Oldest")],
                    ),
                )
            )

    host = SizeLimitedOlderPageHost()

    result = await read_opencode_history(host, SESSION)

    assert result.available and result.complete
    assert [event.event_id for event in result.events] == ["msg_old", "msg_new"]
    assert host.calls == [
        (50, None),
        (50, "older"),
        (25, "older"),
        (12, "older"),
        (6, "older"),
        (3, "older"),
        (1, "older"),
    ]


@pytest.mark.asyncio
async def test_one_oversized_message_is_an_explicit_failure():
    class AlwaysTooLargeHost:
        def __init__(self):
            self.limits = []

        async def read_messages_page(self, session_id, *, limit, before):
            self.limits.append(limit)
            raise OpenCodeHostError("OpenCode response is too large")

    host = AlwaysTooLargeHost()

    result = await read_opencode_history(host, SESSION, page_size=4)

    assert result.unreachable and not result.complete
    assert result.detail == "response_too_large"
    assert result.events == ()
    assert host.limits == [4, 2, 1]


@pytest.mark.asyncio
async def test_empty_history_is_available_and_complete():
    host = FakeHost({None: ([], None)})

    result = await read_opencode_history(host, SESSION)

    assert result.available
    assert result.complete
    assert result.events == ()
    assert result.history is not None
    assert result.history.events == ()


@pytest.mark.asyncio
async def test_missing_parts_are_reported_as_malformed_history():
    broken = {"info": {"id": "msg_broken", "sessionID": SESSION, "role": "user"}}
    host = FakeHost({None: ([broken], None)})

    result = await read_opencode_history(host, SESSION)

    assert result.outcome == HISTORY_UNREACHABLE
    assert result.detail == "malformed_history"
    assert not result.complete
    assert result.events == ()


@pytest.mark.asyncio
async def test_missing_session_and_temporary_failure_stay_distinct():
    missing = await read_opencode_history(
        FakeHost(error=OpenCodeHttpError(404)), SESSION
    )
    assert missing.outcome == HISTORY_MISSING
    assert missing.detail == "not_found"
    assert not missing.complete
    assert missing.events == ()

    timeout = await read_opencode_history(
        FakeHost(error=OpenCodeTimeout("slow")), SESSION
    )
    assert timeout.outcome == HISTORY_UNREACHABLE
    assert timeout.detail == "timeout"
    assert not timeout.complete

    unreachable = await read_opencode_history(
        FakeHost(error=OpenCodeHostError("OpenCode server unreachable")), SESSION
    )
    assert unreachable.outcome == HISTORY_UNREACHABLE
    assert unreachable.detail == "unreachable"
    assert not unreachable.complete

    http = await read_opencode_history(FakeHost(error=OpenCodeHttpError(500)), SESSION)
    assert http.outcome == HISTORY_UNREACHABLE
    assert http.detail == "http_500"
    assert not http.complete

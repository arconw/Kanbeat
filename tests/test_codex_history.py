from copy import deepcopy

import pytest

from kanbeat.codex_history import CodexHistoryReader, read_codex_history
from kanbeat.codex_host import CodexDisconnected, CodexRpcError, CodexTimeout
from kanbeat.session_transfer import TransferLedger, render_short_prompt

THREAD = "01a18660-9ca4-71c0-ba25-81426f4ca65d"


def entry(identifier, kind, **fields):
    return {
        "turnId": "turn-fixture",
        "item": {"id": identifier, "type": kind, **fields},
        "startedAtMs": None,
        "completedAtMs": None,
    }


def user(identifier, text, client_id=None):
    return entry(
        identifier,
        "userMessage",
        content=[{"type": "text", "text": text, "textElements": []}],
        clientId=client_id,
    )


def page(*rows, cursor=None):
    return {"data": list(rows), "nextCursor": cursor, "backwardsCursor": None}


class FakeHost:
    def __init__(self, *responses):
        self.responses = responses
        self.calls = []

    async def request(self, method, params):
        assert method == "thread/items/list"
        self.calls.append((method, deepcopy(params)))
        response = self.responses[len(self.calls) - 1]
        if isinstance(response, BaseException):
            raise response
        return deepcopy(response)


@pytest.mark.asyncio
async def test_paging_preserves_stored_order_ids_roles_and_tool_labels(caplog):
    first = page(
        user("user-1", "Fixture question"),
        entry("assistant-1", "agentMessage", text="Fixture reply"),
        cursor="opaque-next",
    )
    second = page(
        entry(
            "command-1",
            "commandExecution",
            command="pwd",
            cwd="/fixture",
            status="completed",
            aggregatedOutput="/fixture",
            exitCode=0,
        ),
        entry(
            "output-1",
            "functionCallOutput",
            name="read",
            namespace=None,
            output=[{"type": "input_text", "text": "Fixture contents"}],
        ),
    )
    original = deepcopy((first, second))
    host = FakeHost(first, second)
    reader = CodexHistoryReader(host, page_size=2)
    assert not host.calls
    result = await reader.read(THREAD, ordinal_start=7)
    assert result.available and result.complete and result.error is None
    assert [event.event_id for event in result.events] == [
        "user-1",
        "assistant-1",
        "command-1",
        "output-1",
    ]
    assert [event.ordinal for event in result.events] == [7, 8, 9, 10]
    assert [event.role for event in result.events] == [
        "user",
        "assistant",
        "tool_call",
        "tool_result",
    ]
    assert result.events[2].render() == "Tool call (command): pwd\n\nOutput:\n/fixture"
    assert result.events[3].render() == "Tool result (read): Fixture contents"
    assert host.calls == [
        (
            "thread/items/list",
            {"threadId": THREAD, "limit": 2, "sortDirection": "asc"},
        ),
        (
            "thread/items/list",
            {
                "threadId": THREAD,
                "limit": 2,
                "sortDirection": "asc",
                "cursor": "opaque-next",
            },
        ),
    ]
    assert (first, second) == original
    assert not caplog.records
    repeated = await read_codex_history(FakeHost(first, second), THREAD)
    assert [event.key for event in repeated.events] == [
        event.key for event in result.events
    ]


@pytest.mark.asyncio
async def test_import_marker_and_client_id_are_recognized():
    marker = render_short_prompt("Read the fixture context.", "import-1", ["pi"], 0, 2)
    result = await read_codex_history(
        FakeHost(
            page(
                user("marker-1", marker),
                user("client-1", "Imported context", "import-2"),
                entry("reply-1", "agentMessage", text="New reply"),
            )
        ),
        THREAD,
        import_message_ids={"import-2"},
    )
    assert [(event.is_import, event.import_id) for event in result.events] == [
        (True, "import-1"),
        (True, "import-2"),
        (False, None),
    ]


@pytest.mark.asyncio
async def test_ordinary_client_message_id_does_not_drop_original_user_text():
    result = await read_codex_history(
        FakeHost(page(user("user-1", "Original question", "ordinary-ui-message-id"))),
        THREAD,
    )
    assert not result.events[0].is_import
    assert TransferLedger().plan([result.history], "pi").events == result.events


@pytest.mark.asyncio
async def test_unsupported_content_is_named_and_never_exported():
    result = await read_codex_history(
        FakeHost(
            page(
                entry("reason-1", "reasoning", summary=["HIDDEN"], content=["HIDDEN"]),
                entry("image-1", "imageView", path="/fixture/image.png"),
                entry("hook-1", "hookPrompt", fragments=[]),
                entry(
                    "output-1",
                    "functionCallOutput",
                    name="read",
                    output=[
                        {"type": "input_text", "text": "Visible"},
                        {"type": "encrypted_content", "encrypted_content": "HIDDEN"},
                        {"type": "input_image", "image_url": "data:fixture"},
                    ],
                ),
            )
        ),
        THREAD,
    )
    assert result.complete
    assert set(result.unsupported) == {
        "reasoning",
        "attachments",
        "hook prompts",
        "encrypted content",
    }
    assert [event.text for event in result.events] == ["Visible"]


@pytest.mark.asyncio
async def test_unknown_item_types_and_unlabelled_output_use_fixed_categories():
    long_kind = "future-" + "x" * 500
    result = await read_codex_history(
        FakeHost(
            page(
                entry("future-1", long_kind),
                entry(
                    "image-output",
                    "functionCallOutput",
                    name="read",
                    output={"type": "input_image", "image_url": "data:fixture"},
                ),
                entry(
                    "odd-output",
                    "functionCallOutput",
                    name="read",
                    output={"unexpected": "fixture"},
                ),
            )
        ),
        THREAD,
    )
    assert result.available and result.complete
    assert result.events == ()
    assert result.unsupported == ("attachments", "unknown content")
    assert long_kind not in result.unsupported


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (CodexRpcError(-32600, f"thread not found: {THREAD}"), "missing"),
        (CodexRpcError(-32600, f"no rollout found for thread id {THREAD}"), "missing"),
        (CodexRpcError(-32603, "server unavailable"), "unreachable"),
        (CodexDisconnected("disconnected"), "unreachable"),
        (CodexTimeout("timeout"), "unreachable"),
        (OSError("transport error"), "unreachable"),
    ],
)
async def test_missing_and_temporary_failure_remain_distinct_after_partial_page(
    error, outcome
):
    result = await read_codex_history(
        FakeHost(page(user("user-1", "Fixture"), cursor="next"), error), THREAD
    )
    assert result.outcome == outcome
    assert not result.complete and not result.events and result.error


@pytest.mark.asyncio
async def test_empty_history_and_page_limit_are_explicit():
    empty = await read_codex_history(FakeHost(page()), THREAD)
    assert empty.available and empty.complete and not empty.events
    limited = await read_codex_history(
        FakeHost(page(user("user-1", "Fixture"), cursor="next")), THREAD, max_pages=1
    )
    assert limited.available and not limited.complete and limited.error
    assert len(limited.events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        {"data": "invalid"},
        page({}),
        page(entry("item-1", "")),
        page(entry("item-1", "  ")),
    ],
)
async def test_malformed_history_is_not_reported_as_complete(response):
    result = await read_codex_history(FakeHost(response), THREAD)
    assert result.unreachable and not result.complete and not result.events


@pytest.mark.asyncio
async def test_file_change_keeps_operation_and_rename_destination():
    result = await read_codex_history(
        FakeHost(
            page(
                entry(
                    "patch-1",
                    "fileChange",
                    status="completed",
                    changes=[
                        {
                            "path": "old.txt",
                            "kind": {"type": "update", "movePath": "new.txt"},
                            "diff": "-old\n+new",
                        }
                    ],
                )
            )
        ),
        THREAD,
    )
    assert result.complete
    assert "update" in result.events[0].text
    assert "old.txt" in result.events[0].text
    assert "new.txt" in result.events[0].text


@pytest.mark.asyncio
async def test_repeated_page_cursor_fails_without_returning_duplicate_events():
    repeated = page(user("user-1", "Fixture question"), cursor="same-cursor")
    host = FakeHost(repeated, repeated, repeated)
    result = await read_codex_history(host, THREAD, max_pages=3)
    assert result.unreachable
    assert not result.complete and not result.events
    assert len(host.calls) == 2

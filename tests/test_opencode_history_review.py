from copy import deepcopy

import pytest

from kanbeat.opencode_history import read_opencode_history
from kanbeat.opencode_host import (
    OpenCodeHostError,
    OpenCodeHttpError,
    OpenCodeMessagePage,
    OpenCodeServeHost,
)
from kanbeat.session_transfer import render_short_prompt


SESSION = "ses_reviewfixture"


def row(identifier, role, text):
    return {
        "info": {"id": identifier, "sessionID": SESSION, "role": role},
        "parts": [{"id": f"prt_{identifier}", "type": "text", "text": text}],
    }


class ReadOnlyHost:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.calls = []

    async def read_messages_page(self, session_id, *, limit, before):
        self.calls.append((session_id, limit, before))
        page = next(self.pages)
        if isinstance(page, Exception):
            raise page
        return page


@pytest.mark.asyncio
async def test_review_reads_on_request_without_mutation_or_content_logs(caplog, capsys):
    messages = [
        row("msg_user", "user", "fixture question"),
        row("msg_answer", "assistant", "fixture answer"),
    ]
    original = deepcopy(messages)
    host = ReadOnlyHost([OpenCodeMessagePage(tuple(messages))])
    request = read_opencode_history(host, SESSION, ordinal_start=12)
    assert host.calls == []

    result = await request

    assert result.available and result.complete
    assert [event.ordinal for event in result.events] == [12, 13]
    assert [event.event_id for event in result.events] == ["msg_user", "msg_answer"]
    assert messages == original
    assert caplog.text == ""
    assert capsys.readouterr() == ("", "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [OpenCodeHttpError(404), OpenCodeHostError("offline")]
)
async def test_review_later_page_failure_never_returns_partial_success(error):
    host = ReadOnlyHost(
        [OpenCodeMessagePage((row("msg_latest", "user", "latest"),), "older"), error]
    )

    result = await read_opencode_history(host, SESSION)

    assert not result.available and not result.complete
    assert result.events == ()
    assert result.missing == isinstance(error, OpenCodeHttpError)


@pytest.mark.asyncio
async def test_review_repeated_cursor_is_explicit_failure():
    host = ReadOnlyHost([OpenCodeMessagePage((), "same")] * 2)

    result = await read_opencode_history(host, SESSION)

    assert result.unreachable and not result.complete
    assert result.detail == "paging_loop"
    assert len(host.calls) == 2


@pytest.mark.asyncio
async def test_review_marker_split_across_user_parts_is_recognized():
    text = render_short_prompt("Read fixture context", "review-import", ["codex"], 0, 2)
    message = row("msg_import", "user", text)
    message["parts"] = [
        {"id": f"prt_{index}", "type": "text", "text": line}
        for index, line in enumerate(text.splitlines())
    ]
    host = ReadOnlyHost([OpenCodeMessagePage((message,))])

    result = await read_opencode_history(host, SESSION)

    assert result.events[0].is_import
    assert result.events[0].import_id == "review-import"


@pytest.mark.asyncio
@pytest.mark.parametrize("parts", [None, "broken", [None]])
async def test_review_malformed_parts_are_not_reported_as_complete(parts):
    message = row("msg_broken", "user", "fixture")
    message["parts"] = parts
    host = ReadOnlyHost([OpenCodeMessagePage((message,))])

    result = await read_opencode_history(host, SESSION)

    assert result.unreachable and not result.complete
    assert result.detail == "malformed_history"


@pytest.mark.asyncio
async def test_review_missing_parts_are_not_silently_replaced_with_empty_history():
    message = row("msg_broken", "user", "fixture")
    del message["parts"]
    host = ReadOnlyHost([OpenCodeMessagePage((message,))])

    result = await read_opencode_history(host, SESSION)

    assert result.unreachable and not result.complete
    assert result.detail == "malformed_history"


@pytest.mark.asyncio
async def test_review_file_reference_survives_without_copying_binary_payload():
    message = row("msg_file", "user", "Inspect the attached fixture")
    message["parts"].append(
        {
            "id": "prt_file",
            "type": "file",
            "mime": "image/png",
            "filename": "fixture-diagram.png",
            "url": "data:image/png;base64,AAAA",
        }
    )
    host = ReadOnlyHost([OpenCodeMessagePage((message,))])

    result = await read_opencode_history(host, SESSION)

    assert "file" in result.unsupported
    exported = "\n".join(event.text for event in result.events)
    assert "fixture-diagram.png" in exported
    assert "AAAA" not in exported


@pytest.mark.asyncio
async def test_review_host_requests_paged_get_and_preserves_cursor(monkeypatch):
    calls = []

    async def request(self, method, path, body=None, *, query=None):
        calls.append((method, path, body, query))
        return [], {"x-next-cursor": "older-cursor"}

    monkeypatch.setattr(OpenCodeServeHost, "_call_with_headers", request)
    host = object.__new__(OpenCodeServeHost)

    page = await host.read_messages_page(SESSION, limit=3, before="opaque+/=")

    assert page.next_cursor == "older-cursor"
    assert calls == [
        (
            "GET",
            f"/session/{SESSION}/message",
            None,
            {"limit": "3", "before": "opaque+/="},
        )
    ]

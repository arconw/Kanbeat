import asyncio
import json
import os
import stat
import sys
import uuid
from types import SimpleNamespace

import pytest

from kanbeat.codex_host import CodexAppServerHost, CodexHostError, CodexRpcError
from kanbeat.opencode_host import (
    OpenCodeHostError,
    OpenCodeHttpError,
    OpenCodeMessagePage,
    OpenCodeServeHost,
)
from kanbeat.session_groups import SessionGroups
from kanbeat.session_switch import (
    SessionSwitchRefused,
    _ensure_no_native_writer,
    _one_shot,
    _prompt_event_key,
    switch_harness,
)
from kanbeat.session_transfer import (
    IMPORT_NOTE,
    ConversationEvent,
    find_import_markers,
)

THREAD = "01a18660-9ca4-71c0-ba25-81426f4ca65d"


def thread_id(index: int) -> str:
    return "01a18660-9ca4-71c0-ba25-" + f"{index:012x}"


def codex_entry(identifier, kind, **fields):
    return {
        "turnId": "turn-fixture",
        "item": {"id": identifier, "type": kind, **fields},
        "startedAtMs": None,
        "completedAtMs": None,
    }


def codex_user(identifier, text, client_id=None):
    return codex_entry(
        identifier,
        "userMessage",
        content=[{"type": "text", "text": text, "textElements": []}],
        clientId=client_id,
    )


def codex_assistant(identifier, text):
    return codex_entry(identifier, "agentMessage", text=text)


def opencode_message(identifier, role, text):
    return {
        "info": {"id": identifier, "sessionID": "ses_0", "role": role},
        "parts": [
            {
                "id": "prt-" + identifier,
                "sessionID": "ses_0",
                "messageID": identifier,
                "type": "text",
                "text": text,
            }
        ],
    }


class FakeCodex:
    def __init__(self):
        self.threads = {}
        self.statuses = {}
        self.turns = []
        self.created = []
        self.observed = []
        self.deleted = []
        self.unreachable = False

    async def request(self, method, params):
        assert method == "thread/items/list"
        if self.unreachable:
            raise CodexHostError("Codex app-server is unreachable")
        thread = params["threadId"]
        if thread not in self.threads:
            raise CodexRpcError(-32600, f"thread not found: {thread}")
        return {
            "data": list(self.threads[thread]),
            "nextCursor": None,
            "backwardsCursor": None,
        }

    async def start_thread(self, cwd, model=None, model_provider=None):
        identifier = thread_id(len(self.created) + 1)
        self.created.append(identifier)
        self.threads[identifier] = []
        return SimpleNamespace(thread_id=identifier)

    async def set_thread_name(self, thread_id, name):
        pass

    async def read_thread(self, thread_id):
        if thread_id not in self.threads:
            raise CodexRpcError(-32600, f"thread not found: {thread_id}")
        return SimpleNamespace(status=self.statuses.get(thread_id, "idle"))

    async def observe_thread(self, thread_id):
        if thread_id not in self.threads:
            raise CodexRpcError(-32600, f"thread not found: {thread_id}")
        self.observed.append(thread_id)
        return SimpleNamespace(status=self.statuses.get(thread_id, "idle"))

    async def start_turn(self, thread_id, text, *, client_user_message_id=None):
        self.turns.append(
            {
                "thread_id": thread_id,
                "text": text,
                "client_user_message_id": client_user_message_id,
            }
        )
        identifier = f"item-{len(self.threads[thread_id])}"
        self.threads[thread_id].append(
            codex_user(identifier, text, client_id=client_user_message_id)
        )
        return {"id": "turn-started"}

    async def delete_thread(self, thread_id):
        self.deleted.append(thread_id)
        self.threads.pop(thread_id, None)


class FakeOpenCode:
    def __init__(self):
        self.sessions = {}
        self.messages = {}
        self.busy = set()
        self.sent = []
        self.deleted = []
        self.created = []
        self.fail_create = False
        self.fail_send_before = None
        self.fail_send_after = False

    async def create_session(self, *, directory=None, model=None):
        if self.fail_create:
            raise OpenCodeHostError("OpenCode could not create a session")
        identifier = "ses_" + str(len(self.created))
        self.created.append(identifier)
        self.sessions[identifier] = True
        self.messages[identifier] = []
        return SimpleNamespace(session_id=identifier)

    async def read_session(self, session_id):
        if session_id not in self.sessions:
            raise OpenCodeHttpError(404)
        return SimpleNamespace(session_id=session_id)

    async def delete_session(self, session_id):
        self.deleted.append(session_id)
        if session_id not in self.sessions:
            raise OpenCodeHttpError(404)
        self.sessions.pop(session_id)

    async def statuses(self):
        return {session_id: SimpleNamespace(status="busy") for session_id in self.busy}

    async def read_messages_page(self, session_id, *, limit=50, before=None):
        if session_id not in self.sessions:
            raise OpenCodeHttpError(404)
        return OpenCodeMessagePage(tuple(self.messages[session_id]), None)

    async def send_message(
        self,
        session_id,
        *,
        message_id,
        text,
        system=None,
        no_reply=True,
    ):
        if self.fail_send_before is not None:
            raise OpenCodeHttpError(self.fail_send_before)
        self.sent.append(
            {
                "session_id": session_id,
                "message_id": message_id,
                "text": text,
                "system": system,
                "no_reply": no_reply,
            }
        )
        self.messages[session_id].append(opencode_message(message_id, "user", text))
        if self.fail_send_after:
            raise OpenCodeHttpError(500)
        return {"info": {"id": message_id}}

    def add_assistant(self, session_id, identifier, text):
        self.messages[session_id].append(
            opencode_message(identifier, "assistant", text)
        )


class FakeState:
    def __init__(self, root):
        self.board = SimpleNamespace(root=root / ".kanbeat", project=root)
        self.data = {}
        self.saves = 0

    def save(self):
        self.saves += 1


class FakeGroups:
    def __init__(self, root):
        self.state = FakeState(root)
        self.entries = {}
        self.codex = FakeCodex()
        self.opencode = FakeOpenCode()
        self.claude_runtime = None
        self.claude_projects = root / "claude-projects"
        self.pi_sessions = root / "pi-sessions"

        async def codex_host():
            return self.codex

        async def opencode_host():
            return self.opencode

        self.codex_host = codex_host
        self.opencode_host = opencode_host

    def custom(self, terminal_id):
        entry = self.entries.get(terminal_id)
        if entry is None or entry.get("owner") != "custom":
            raise ValueError("Unknown independent session")
        return entry

    def inactive(self, entry):
        if entry.get("status") in (
            "starting",
            "running",
            "stopping",
            "recovery_blocked",
        ):
            raise ValueError("Stop the session and resolve orphan recovery first")

    def harness_available(self, harness):
        return True

    def fresh_settings(self, harness):
        return None

    async def create_codex_thread(self, host, name, settings=None):
        return await host.start_thread(self.state.board.project)

    async def create_opencode_session(self, host, model=None):
        return await host.create_session(
            directory=self.state.board.project, model=model
        )

    def claude_projects_dir(self):
        return self.claude_projects

    def claude_session_started(self, session_id):
        return any(self.claude_projects.glob(f"*/{session_id}.jsonl"))

    def pi_sessions_dir(self):
        return self.pi_sessions


def entry_for(harness="codex", native=THREAD, status="stopped", **fields):
    entry = {
        "id": "t1",
        "owner": "custom",
        "harness": harness,
        "native_session": native,
        "status": status,
        "title": "Fixture session",
        "role": "",
    }
    entry.update(fields)
    return entry


def make_groups(root):
    groups = FakeGroups(root)
    groups.codex.threads[THREAD] = [
        codex_user("codex-user-1", "Codex question"),
        codex_assistant("codex-assistant-1", "Codex answer"),
    ]
    groups.entries["t1"] = entry_for()
    return groups


def restart(entry):
    return json.loads(json.dumps(entry))


def start_marker_count(text):
    return text.count("<!-- kanbeat-import id=")


def write_claude_transcript(groups, session_id, prompt, replies=()):
    directory = groups.claude_projects_dir() / "-fixture-project"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.jsonl"
    records = []
    if path.exists():
        records = [json.loads(line) for line in path.read_text().splitlines()]
    records.append(
        {
            "uuid": uuid.uuid4().hex,
            "parentUuid": records[-1]["uuid"] if records else None,
            "sessionId": session_id,
            "isSidechain": False,
            "type": "user",
            "message": {"role": "user", "content": prompt},
            "timestamp": "2030-01-01T00:00:00Z",
        }
    )
    for index, text in enumerate(replies):
        records.append(
            {
                "uuid": uuid.uuid4().hex,
                "parentUuid": records[-1]["uuid"],
                "sessionId": session_id,
                "isSidechain": False,
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": text}],
                    "stop_reason": "end_turn",
                },
                "timestamp": "2030-01-01T00:00:%02dZ" % (index + 1),
            }
        )
    path.write_text("".join(json.dumps(row) + "\n" for row in records))
    return path


def write_pi_session(groups, session_id, prompt, replies=()):
    directory = groups.pi_sessions_dir() / "--fixture-project--"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"2030-01-01T00-00-00-000Z_{session_id}.jsonl"
    records = []
    if path.exists():
        records = [json.loads(line) for line in path.read_text().splitlines()]
    if not records:
        records.append(
            {
                "type": "session",
                "version": 3,
                "id": session_id,
                "timestamp": "2030-01-01T00:00:00.000Z",
                "cwd": "/fixture/project",
            }
        )
    records.append(
        {
            "type": "message",
            "id": uuid.uuid4().hex[:8],
            "parentId": records[-1]["id"] if len(records) > 1 else None,
            "timestamp": "2030-01-01T00:00:01.000Z",
            "message": {"role": "user", "content": prompt},
        }
    )
    previous = records[-1]["id"]
    for index, text in enumerate(replies):
        identifier = uuid.uuid4().hex[:8]
        records.append(
            {
                "type": "message",
                "id": identifier,
                "parentId": previous,
                "timestamp": "2030-01-01T00:00:%02d.000Z" % (index + 2),
                "message": {"role": "assistant", "content": text, "stopReason": "stop"},
            }
        )
        previous = identifier
    path.write_text("".join(json.dumps(row) + "\n" for row in records))
    return path


def append_claude_reply(groups, session_id, text):
    path = groups.claude_projects_dir() / "-fixture-project" / f"{session_id}.jsonl"
    records = [json.loads(line) for line in path.read_text().splitlines()]
    records.append(
        {
            "uuid": uuid.uuid4().hex,
            "parentUuid": records[-1]["uuid"],
            "sessionId": session_id,
            "isSidechain": False,
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
                "stop_reason": "end_turn",
            },
            "timestamp": "2030-01-01T00:01:00Z",
        }
    )
    path.write_text("".join(json.dumps(row) + "\n" for row in records))


def append_pi_reply(groups, session_id, text):
    directory = groups.pi_sessions_dir() / "--fixture-project--"
    path = next(directory.glob(f"*_{session_id}.jsonl"))
    records = [json.loads(line) for line in path.read_text().splitlines()]
    records.append(
        {
            "type": "message",
            "id": uuid.uuid4().hex[:8],
            "parentId": records[-1]["id"],
            "timestamp": "2030-01-01T00:01:00.000Z",
            "message": {"role": "assistant", "content": text, "stopReason": "stop"},
        }
    )
    path.write_text("".join(json.dumps(row) + "\n" for row in records))


def claude_arguments(argv):
    session_id = (
        argv[argv.index("--session-id") + 1]
        if "--session-id" in argv
        else argv[argv.index("--resume") + 1]
    )
    return session_id, argv[argv.index("-p") + 1]


def claude_one_shot(groups, captured):
    async def one_shot(argv, cwd, environment):
        session_id, prompt = claude_arguments(argv)
        directory = groups.state.board.root / ".state" / "transfers" / "t1"
        files = sorted(directory.iterdir()) if directory.is_dir() else []
        captured.append(
            {
                "argv": list(argv),
                "prompt": prompt,
                "session_id": session_id,
                "context": files[0].read_text() if files else None,
                "context_mode": (
                    stat.S_IMODE(files[0].stat().st_mode) if files else None
                ),
            }
        )
        write_claude_transcript(groups, session_id, prompt)
        return 0, ""

    return one_shot


def pi_one_shot(groups, captured):
    async def one_shot(argv, cwd, environment):
        session_id = argv[argv.index("--session-id") + 1]
        prompt = argv[-1]
        directory = groups.state.board.root / ".state" / "transfers" / "t1"
        files = sorted(directory.iterdir()) if directory.is_dir() else []
        captured.append(
            {
                "argv": list(argv),
                "prompt": prompt,
                "session_id": session_id,
                "context": files[0].read_text() if files else None,
                "context_mode": (
                    stat.S_IMODE(files[0].stat().st_mode) if files else None
                ),
            }
        )
        write_pi_session(groups, session_id, prompt)
        return 0, ""

    return one_shot


@pytest.fixture
def installed(monkeypatch):
    monkeypatch.setattr("kanbeat.session_switch.available", lambda name: True)


@pytest.mark.asyncio
async def test_codex_to_opencode_to_codex_delivers_only_new_events(tmp_path, installed):
    groups = make_groups(tmp_path)

    first = await switch_harness(groups, {"terminal": "t1", "harness": "opencode"})
    entry = groups.entries["t1"]
    opencode_id = first["native_session"]
    assert first["changed"] is True
    assert first["delivered"] == 2
    assert entry["harness"] == "opencode"
    assert entry["native_session"] == opencode_id
    assert {
        harness: row["id"] for harness, row in entry["native_sessions"].items()
    } == {
        "codex": THREAD,
        "opencode": opencode_id,
    }
    (sent,) = groups.opencode.sent
    assert sent["session_id"] == opencode_id
    assert sent["message_id"] == "msg" + first["import_id"]
    assert sent["no_reply"] is True
    assert sent["system"] == IMPORT_NOTE
    assert "Codex question" in sent["text"]
    assert "Codex answer" in sent["text"]
    assert IMPORT_NOTE in sent["text"]
    assert sent["text"].rstrip().endswith("-->")
    assert start_marker_count(sent["text"]) == 1

    groups.opencode.add_assistant(opencode_id, "msg_reply_1", "OpenCode answer")
    second = await switch_harness(groups, {"terminal": "t1", "harness": "codex"})
    assert second["delivered"] == 1
    assert second["native_session"] == THREAD
    assert entry["harness"] == "codex"
    (turn,) = groups.codex.turns
    assert turn["thread_id"] == THREAD
    assert turn["client_user_message_id"] == second["target_message_id"]
    assert "OpenCode answer" in turn["text"]
    assert "Codex question" not in turn["text"]
    assert start_marker_count(turn["text"]) == 1

    before = len(groups.codex.turns)
    third = await switch_harness(groups, {"terminal": "t1", "harness": "opencode"})
    assert third["delivered"] == 0
    assert third["native_session"] == opencode_id
    assert third["changed"] is True
    assert len(groups.codex.turns) == before
    assert len(groups.opencode.sent) == 1
    assert entry["harness"] == "opencode"


@pytest.mark.asyncio
async def test_a_switch_does_not_replay_the_stored_initial_prompt(tmp_path, installed):
    groups = make_groups(tmp_path)
    groups.entries["t1"]["prompt"] = "Initial instructions"
    groups.codex.threads[THREAD].insert(
        0,
        codex_user(
            "codex-prompt", "Kanbeat interactive instructions:\nInitial instructions"
        ),
    )
    await switch_harness(groups, {"terminal": "t1", "harness": "opencode"})
    assert groups.entries["t1"]["harness"] == "opencode"
    assert "prompt" not in groups.entries["t1"]

    empty = make_groups(tmp_path / "empty")
    empty.codex.threads[THREAD] = []
    empty.entries["t1"]["prompt"] = "Unsent instructions"
    result = await switch_harness(empty, {"terminal": "t1", "harness": "opencode"})
    assert result["delivered"] == 0
    assert empty.entries["t1"]["prompt"] == "Unsent instructions"


@pytest.mark.asyncio
async def test_a_reduction_keeps_the_initial_prompt_until_it_is_transferred(
    tmp_path, installed
):
    groups = make_groups(tmp_path)
    groups.entries["t1"]["prompt"] = "Initial instructions"
    groups.codex.threads[THREAD].insert(
        0,
        codex_user(
            "codex-prompt", "Kanbeat interactive instructions:\nInitial instructions"
        ),
    )
    result = await switch_harness(
        groups,
        {
            "terminal": "t1",
            "harness": "opencode",
            "reduction": {"kind": "newest", "count": 2},
        },
    )
    assert result["delivered"] == 2
    assert groups.entries["t1"]["prompt"] == "Initial instructions"


def test_prompt_matching_needs_the_whole_initial_message():
    follow_up = ConversationEvent(
        harness="codex",
        ordinal=0,
        role="user",
        text="Kanbeat interactive instructions:\nInitial instructions continued",
        event_id="follow-up",
    )
    reads = [SimpleNamespace(events=(follow_up,))]
    assert _prompt_event_key({"prompt": "Initial instructions"}, reads) is None


@pytest.mark.asyncio
async def test_switching_back_to_the_harness_that_has_the_prompt_clears_it(
    tmp_path, installed
):
    groups = FakeGroups(tmp_path)
    groups.codex.threads[THREAD] = [
        codex_user(
            "codex-prompt", "Kanbeat interactive instructions:\nInitial instructions"
        ),
        codex_assistant("codex-answer", "Earlier Codex answer"),
    ]
    groups.opencode.sessions["ses_0"] = True
    groups.opencode.messages["ses_0"] = [
        opencode_message("msg_1", "user", "OpenCode question"),
        opencode_message("msg_2", "assistant", "OpenCode answer"),
    ]
    groups.entries["t1"] = entry_for(
        harness="opencode",
        native="ses_0",
        prompt="Initial instructions",
        native_sessions={
            "codex": {"id": THREAD, "status": "available"},
            "opencode": {"id": "ses_0", "status": "available"},
        },
    )
    result = await switch_harness(groups, {"terminal": "t1", "harness": "codex"})
    assert result["delivered"] == 2
    assert groups.entries["t1"]["harness"] == "codex"
    assert "prompt" not in groups.entries["t1"]


@pytest.mark.asyncio
async def test_three_harness_cycles_keep_checkpoints_and_do_not_nest_envelopes(
    tmp_path, installed, monkeypatch
):
    groups = make_groups(tmp_path)
    captured = []
    monkeypatch.setattr(
        "kanbeat.session_switch._one_shot", claude_one_shot(groups, captured)
    )

    opencode = await switch_harness(groups, {"terminal": "t1", "harness": "opencode"})
    opencode_id = opencode["native_session"]
    groups.opencode.add_assistant(opencode_id, "msg_reply_1", "OpenCode reply one")

    claude = await switch_harness(groups, {"terminal": "t1", "harness": "claude"})
    claude_id = claude["native_session"]
    assert claude["delivered"] == 3
    context = captured[-1]["context"]
    assert context is not None
    assert captured[-1]["context_mode"] == 0o600
    assert len(find_import_markers(context)) == 1
    assert start_marker_count(context) == 1
    assert "Codex question" in context
    assert "OpenCode reply one" in context
    assert "Codex question" not in captured[-1]["argv"][-1]
    assert IMPORT_NOTE in captured[-1]["argv"][-1]
    assert captured[-1]["argv"][-1].rstrip().endswith("-->")

    append_claude_reply(groups, claude_id, "Claude reply")

    back = await switch_harness(groups, {"terminal": "t1", "harness": "codex"})
    assert back["delivered"] == 2
    assert "OpenCode reply one" in groups.codex.turns[-1]["text"]
    assert "Claude reply" in groups.codex.turns[-1]["text"]
    assert "Codex question" not in groups.codex.turns[-1]["text"]
    assert start_marker_count(groups.codex.turns[-1]["text"]) == 1
    assert len(find_import_markers(groups.codex.turns[-1]["text"])) == 1

    groups.codex.threads[THREAD].append(
        codex_assistant("codex-assistant-2", "Codex reply two")
    )
    groups.opencode.add_assistant(opencode_id, "msg_reply_2", "OpenCode reply two")

    cycle = await switch_harness(groups, {"terminal": "t1", "harness": "opencode"})
    assert cycle["delivered"] == 2
    assert "Codex reply two" in groups.opencode.sent[-1]["text"]
    assert "Claude reply" in groups.opencode.sent[-1]["text"]
    assert start_marker_count(groups.opencode.sent[-1]["text"]) == 1

    captured.clear()
    cycle = await switch_harness(groups, {"terminal": "t1", "harness": "claude"})
    assert cycle["delivered"] == 2
    context = captured[-1]["context"]
    assert "Codex reply two" in context
    assert "OpenCode reply two" in context
    assert "Claude reply" not in context
    assert start_marker_count(context) == 1
    assert len(find_import_markers(context)) == 1
    append_claude_reply(groups, claude_id, "Claude reply two")

    await switch_harness(groups, {"terminal": "t1", "harness": "codex"})
    final = await switch_harness(groups, {"terminal": "t1", "harness": "claude"})
    assert final["delivered"] == 0
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_pi_delivery_uses_a_private_context_file_and_resumes_session(
    tmp_path, installed, monkeypatch
):
    groups = make_groups(tmp_path)
    captured = []
    monkeypatch.setattr(
        "kanbeat.session_switch._one_shot", pi_one_shot(groups, captured)
    )

    result = await switch_harness(groups, {"terminal": "t1", "harness": "pi"})
    entry = groups.entries["t1"]
    pi_id = result["native_session"]
    (call,) = captured
    assert call["argv"][:2] == ["pi", "-p"]
    assert call["argv"][-1] == call["prompt"]
    assert "Codex question" not in call["prompt"]
    assert IMPORT_NOTE in call["prompt"]
    assert call["prompt"].rstrip().endswith("-->")
    assert pi_id in call["argv"]
    assert call["context_mode"] == 0o600
    assert "Codex question" in call["context"]
    assert not (groups.state.board.root / ".state" / "transfers" / "t1").exists()
    path = next(groups.pi_sessions_dir().glob(f"*/*_{pi_id}.jsonl"))
    records = [json.loads(line) for line in path.read_text().splitlines()]
    user = next(
        row
        for row in records
        if row["type"] == "message" and row["message"]["role"] == "user"
    )
    assert result["target_message_id"] == user["id"]
    assert entry["harness"] == "pi"

    append_pi_reply(groups, pi_id, "Pi reply")
    back = await switch_harness(groups, {"terminal": "t1", "harness": "codex"})
    assert back["delivered"] == 1
    assert "Pi reply" in groups.codex.turns[-1]["text"]
    assert "Codex question" not in groups.codex.turns[-1]["text"]
    assert start_marker_count(groups.codex.turns[-1]["text"]) == 1


@pytest.mark.asyncio
async def test_missing_target_session_needs_explicit_fresh_and_transient_does_not_replace(
    tmp_path, installed
):
    groups = make_groups(tmp_path)
    first = await switch_harness(groups, {"terminal": "t1", "harness": "opencode"})
    entry = groups.entries["t1"]
    old_id = first["native_session"]
    entry["harness"] = "codex"
    entry["native_session"] = THREAD

    real_read_session = groups.opencode.read_session

    async def unreachable(session_id):
        raise OpenCodeHttpError(503)

    groups.opencode.read_session = unreachable
    with pytest.raises(ValueError, match="retry the switch"):
        await switch_harness(groups, {"terminal": "t1", "harness": "opencode"})
    assert entry["harness"] == "codex"
    assert entry["native_session"] == THREAD
    assert entry["native_sessions"]["opencode"] == {
        "id": old_id,
        "status": "available",
    }
    assert groups.opencode.created == [old_id]

    groups.opencode.read_session = real_read_session
    groups.opencode.sessions.pop(old_id)
    groups.opencode.messages.pop(old_id)

    with pytest.raises(SessionSwitchRefused) as refused:
        await switch_harness(groups, {"terminal": "t1", "harness": "opencode"})
    assert refused.value.reason == "target_session_missing"
    assert refused.value.details["native_session"] == old_id
    assert refused.value.details["options"][0]["kind"] == "fresh_session"
    assert entry["harness"] == "codex"
    assert entry["native_session"] == THREAD
    assert entry["native_sessions"]["opencode"] == {
        "id": old_id,
        "status": "missing",
    }

    fresh = await switch_harness(
        groups,
        {"terminal": "t1", "harness": "opencode", "fresh_session": True},
    )
    assert fresh["fresh_session"] is True
    assert fresh["native_session"] != old_id
    assert fresh["delivered"] == 2
    assert entry["native_sessions"]["opencode"]["id"] == fresh["native_session"]
    assert entry["native_sessions"]["opencode"]["status"] == "available"
    assert len(groups.opencode.sent) == 2
    assert groups.opencode.sent[-1]["text"].count("Codex question") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("store_before_failure", [False, True])
async def test_restart_reconciles_uncertain_delivery_before_retry(
    tmp_path, installed, store_before_failure
):
    groups = make_groups(tmp_path)
    groups.opencode.fail_send_after = store_before_failure
    groups.opencode.fail_send_before = None if store_before_failure else 500

    with pytest.raises(ValueError, match="checks the target history"):
        await switch_harness(groups, {"terminal": "t1", "harness": "opencode"})
    entry = restart(groups.entries["t1"])
    groups.entries["t1"] = entry
    assert entry["harness"] == "codex"
    assert entry["native_session"] == THREAD
    pending = entry["transfer"]["imports"]
    assert [row["status"] for row in pending] == ["uncertain"]
    target_id = entry["native_sessions"]["opencode"]["id"]
    messages = len(groups.opencode.messages[target_id])
    assert messages == (1 if store_before_failure else 0)

    groups.opencode.fail_send_after = False
    groups.opencode.fail_send_before = None
    result = await switch_harness(groups, {"terminal": "t1", "harness": "opencode"})

    statuses = [row["status"] for row in entry["transfer"]["imports"]]
    if store_before_failure:
        assert result["delivered"] == 0
        assert statuses == ["confirmed"]
        assert len(groups.opencode.messages[target_id]) == 1
    else:
        assert result["delivered"] == 2
        assert sorted(statuses) == ["confirmed", "not_delivered"]
        assert len(groups.opencode.messages[target_id]) == 1
    assert entry["harness"] == "opencode"
    assert groups.opencode.sent[-1]["text"].count("Codex question") == 1


@pytest.mark.asyncio
async def test_missing_third_harness_is_skipped_and_stays_skipped(
    tmp_path, installed, monkeypatch
):
    groups = FakeGroups(tmp_path)
    groups.opencode.sessions["ses_source"] = True
    groups.opencode.messages["ses_source"] = [
        opencode_message("msg_user_source", "user", "OpenCode question"),
        opencode_message("msg_reply_source", "assistant", "OpenCode reply"),
    ]
    groups.entries["t1"] = entry_for(
        harness="opencode",
        native="ses_source",
        native_sessions={
            "claude": {"id": "claude-missing", "status": "available"},
        },
    )

    result = await switch_harness(groups, {"terminal": "t1", "harness": "codex"})
    entry = groups.entries["t1"]
    assert result["changed"] is True
    assert result["delivered"] == 2
    assert result["harness"] == "codex"
    assert entry["native_sessions"]["claude"] == {
        "id": "claude-missing",
        "status": "missing",
    }
    assert "OpenCode question" in groups.codex.turns[-1]["text"]
    assert "OpenCode reply" in groups.codex.turns[-1]["text"]

    async def forbidden(*args, **kwargs):
        raise AssertionError("a recorded missing history must not be read again")

    monkeypatch.setattr("kanbeat.session_switch.read_claude_history", forbidden)
    back = await switch_harness(groups, {"terminal": "t1", "harness": "opencode"})
    assert back["delivered"] == 0
    assert back["native_session"] == "ses_source"
    assert entry["native_sessions"]["claude"]["status"] == "missing"


@pytest.mark.asyncio
async def test_read_prepare_and_delivery_failures_leave_source_active(
    tmp_path, installed
):
    groups = make_groups(tmp_path)
    groups.codex.unreachable = True
    with pytest.raises(ValueError, match="temporarily unreachable"):
        await switch_harness(groups, {"terminal": "t1", "harness": "opencode"})
    entry = groups.entries["t1"]
    assert entry["harness"] == "codex"
    assert entry["native_session"] == THREAD
    assert groups.opencode.created == []
    assert "transfer" not in entry

    groups.codex.unreachable = False
    groups.opencode.fail_create = True
    with pytest.raises(ValueError, match="retry the switch after the target"):
        await switch_harness(groups, {"terminal": "t1", "harness": "opencode"})
    assert entry["harness"] == "codex"
    assert entry["native_session"] == THREAD
    assert "opencode" not in entry["native_sessions"]
    assert "transfer" not in entry

    groups.opencode.fail_create = False
    groups.opencode.fail_send_before = 400
    with pytest.raises(ValueError, match="Nothing was delivered"):
        await switch_harness(groups, {"terminal": "t1", "harness": "opencode"})
    assert entry["harness"] == "codex"
    assert entry["native_session"] == THREAD
    assert entry["transfer"]["delivered"] == {}
    assert [row["status"] for row in entry["transfer"]["imports"]] == ["not_delivered"]
    assert groups.opencode.created


@pytest.mark.asyncio
async def test_large_transfer_is_refused_until_an_explicit_reduction_is_chosen(
    tmp_path, installed
):
    groups = FakeGroups(tmp_path)
    groups.codex.threads[THREAD] = [
        codex_assistant(f"assistant-{index}", f"text-{index} " + "x" * 20000)
        for index in range(60)
    ]
    groups.entries["t1"] = entry_for()

    with pytest.raises(SessionSwitchRefused) as refused:
        await switch_harness(groups, {"terminal": "t1", "harness": "opencode"})
    assert refused.value.reason == "transfer_too_large"
    assert "Nothing was truncated" in str(refused.value)
    assert "context window" in str(refused.value)
    assert refused.value.details["size"]["full"]["bytes"] > 1024 * 1024
    kinds = [
        (option["kind"], option["count"]) for option in refused.value.details["options"]
    ]
    assert kinds == [("newest", 50), ("newest", 20), ("newest", 10)]
    assert groups.opencode.created == []
    assert groups.entries["t1"]["harness"] == "codex"

    result = await switch_harness(
        groups,
        {
            "terminal": "t1",
            "harness": "opencode",
            "reduction": {"kind": "newest", "count": 10},
        },
    )
    assert result["delivered"] == 10
    assert result["transfer"]["first_ordinal"] == 50
    assert result["transfer"]["last_ordinal"] == 59
    (sent,) = groups.opencode.sent
    assert "text-50" in sent["text"]
    assert "text-0 " not in sent["text"]
    assert start_marker_count(sent["text"]) == 1


@pytest.mark.asyncio
async def test_context_file_stays_until_an_uncertain_delivery_is_reconciled(
    tmp_path, installed, monkeypatch
):
    groups = make_groups(tmp_path)
    captured = []

    async def uncertain(argv, cwd, environment):
        session_id, prompt = claude_arguments(argv)
        directory = groups.state.board.root / ".state" / "transfers" / "t1"
        files = sorted(directory.iterdir())
        assert len(files) == 1
        captured.append((files[0].read_text(), prompt, session_id))
        write_claude_transcript(groups, session_id, prompt)
        return 1, "the model turn failed after storing the message"

    monkeypatch.setattr("kanbeat.session_switch._one_shot", uncertain)
    with pytest.raises(ValueError, match="Delivery to claude was not confirmed"):
        await switch_harness(groups, {"terminal": "t1", "harness": "claude"})

    entry = groups.entries["t1"]
    assert entry["harness"] == "codex"
    assert [row["status"] for row in entry["transfer"]["imports"]] == ["uncertain"]
    directory = groups.state.board.root / ".state" / "transfers" / "t1"
    files = list(directory.iterdir())
    assert len(files) == 1
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    assert files[0].read_text() == captured[0][0]

    groups.entries["t1"] = restart(entry)
    entry = groups.entries["t1"]

    async def forbidden(*args, **kwargs):
        raise AssertionError("a confirmed import must not be delivered again")

    monkeypatch.setattr("kanbeat.session_switch._one_shot", forbidden)
    result = await switch_harness(groups, {"terminal": "t1", "harness": "claude"})
    assert result["delivered"] == 0
    assert entry["harness"] == "claude"
    assert [row["status"] for row in entry["transfer"]["imports"]] == ["confirmed"]
    assert not directory.exists()


@pytest.mark.asyncio
async def test_scope_and_running_state_are_refused(tmp_path, installed, monkeypatch):
    groups = FakeGroups(tmp_path)
    groups.codex.threads[THREAD] = [
        codex_user("codex-user-1", "Codex question"),
    ]
    groups.entries["t1"] = entry_for(harness="shell", native=None)
    with pytest.raises(ValueError, match="independent agent sessions"):
        await switch_harness(groups, {"terminal": "t1", "harness": "opencode"})

    groups.entries["t1"] = entry_for(role="config")
    with pytest.raises(ValueError, match="configuration or hook"):
        await switch_harness(groups, {"terminal": "t1", "harness": "opencode"})

    groups.entries["t1"] = entry_for(role="hook")
    with pytest.raises(ValueError, match="configuration or hook"):
        await switch_harness(groups, {"terminal": "t1", "harness": "opencode"})

    groups.entries["t1"] = entry_for(owner="board")
    with pytest.raises(ValueError, match="Unknown independent session"):
        await switch_harness(groups, {"terminal": "t1", "harness": "opencode"})

    groups.entries["t1"] = entry_for()
    same = await switch_harness(groups, {"terminal": "t1", "harness": "codex"})
    assert same["changed"] is False
    assert same["reason"] == "already_active"

    groups.entries["t1"] = entry_for(status="running")
    with pytest.raises(ValueError, match="Stop the session"):
        await switch_harness(groups, {"terminal": "t1", "harness": "opencode"})

    groups.entries["t1"] = entry_for()
    groups.codex.statuses[THREAD] = "active"
    with pytest.raises(ValueError, match="Codex native session is still running"):
        await switch_harness(groups, {"terminal": "t1", "harness": "opencode"})

    groups.codex.statuses[THREAD] = "idle"
    groups.opencode.sessions["ses_busy"] = True
    groups.opencode.messages["ses_busy"] = []
    groups.opencode.busy.add("ses_busy")
    groups.entries["t1"] = entry_for(
        harness="opencode",
        native="ses_busy",
        native_sessions={"opencode": {"id": "ses_busy", "status": "available"}},
    )
    with pytest.raises(ValueError, match="OpenCode native session is still running"):
        await switch_harness(groups, {"terminal": "t1", "harness": "codex"})

    groups.opencode.busy.clear()
    groups.entries["t1"] = entry_for()
    groups.harness_available = lambda harness: False
    monkeypatch.setattr("kanbeat.session_switch.available", lambda name: False)
    with pytest.raises(ValueError, match="unavailable"):
        await switch_harness(groups, {"terminal": "t1", "harness": "pi"})


class StubTerminals:
    def __init__(self, entries):
        self.entries = entries
        self.sessions = {}


@pytest.mark.asyncio
async def test_delete_removes_every_native_session_and_remove_keeps_them(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("PI_CODING_AGENT_SESSION_DIR", str(tmp_path / "pi"))
    codex = FakeCodex()
    opencode = FakeOpenCode()
    groups = SessionGroups(
        SimpleNamespace(
            board=SimpleNamespace(root=tmp_path / ".kanbeat", project=tmp_path),
            data={},
            save=lambda: None,
        ),
        StubTerminals({}),
        opencode_host=lambda: _value(opencode),
        codex_host=lambda: _value(codex),
    )
    claude_projects = tmp_path / "claude" / "projects" / "-p"
    claude_projects.mkdir(parents=True)
    (claude_projects / "claude-native.jsonl").write_text("{}\n")
    pi_dir = tmp_path / "pi" / "--p--"
    pi_dir.mkdir(parents=True)
    (pi_dir / "2030-01-01T00-00-00-000Z_pi-native.jsonl").write_text("{}\n")
    entry = entry_for(
        harness="codex",
        native="codex-native",
        native_sessions={
            "codex": {"id": "codex-native", "status": "available"},
            "opencode": {"id": "opencode-native", "status": "available"},
            "claude": {"id": "claude-native", "status": "available"},
            "pi": {"id": "pi-native", "status": "available"},
        },
    )
    groups.terminals.entries["t1"] = entry

    await groups.dispatch({"op": "session_delete", "terminal": "t1", "confirmed": True})
    assert codex.deleted == []
    assert opencode.deleted == []
    assert (claude_projects / "claude-native.jsonl").exists()
    assert (pi_dir / "2030-01-01T00-00-00-000Z_pi-native.jsonl").exists()

    groups.terminals.entries["t2"] = entry_for(
        harness="codex",
        native="codex-native",
        native_sessions={
            "codex": {"id": "codex-native", "status": "available"},
            "opencode": {"id": "opencode-native", "status": "available"},
            "claude": {"id": "claude-native", "status": "available"},
            "pi": {"id": "pi-native", "status": "available"},
        },
    )
    await groups.dispatch(
        {
            "op": "session_delete",
            "terminal": "t2",
            "confirmed": True,
            "native": True,
        }
    )
    assert codex.deleted == ["codex-native"]
    assert opencode.deleted == ["opencode-native"]
    assert not (claude_projects / "claude-native.jsonl").exists()
    assert not (pi_dir / "2030-01-01T00-00-00-000Z_pi-native.jsonl").exists()
    assert "t2" not in groups.terminals.entries


async def _value(value):
    return value


class CaptureCodex(CodexAppServerHost):
    def __init__(self):
        self.calls = []

    async def request(self, method, params):
        self.calls.append((method, params))
        return {"turn": {"id": "turn-started"}}


class CaptureOpenCode(OpenCodeServeHost):
    def __init__(self):
        self.calls = []

    async def _call(self, method, path, body=None, *, query=None):
        self.calls.append((method, path, body, query))
        return {"info": {"id": body["messageID"]}}


@pytest.mark.asyncio
async def test_codex_and_opencode_delivery_parameters_are_explicit():
    codex = CaptureCodex()
    await codex.start_turn(
        THREAD, "Fixture transfer", client_user_message_id="import-1"
    )
    method, params = codex.calls[-1]
    assert method == "turn/start"
    assert params == {
        "threadId": THREAD,
        "input": [{"type": "text", "text": "Fixture transfer"}],
        "clientUserMessageId": "import-1",
    }
    for bad in ("", "x" * 201, "line\nbreak", 7):
        with pytest.raises(CodexHostError):
            await codex.start_turn(THREAD, "Fixture", client_user_message_id=bad)

    opencode = CaptureOpenCode()
    payload = await opencode.send_message(
        "ses_abc",
        message_id="msgfixture",
        text="Fixture transfer",
        system=IMPORT_NOTE,
    )
    assert payload == {"info": {"id": "msgfixture"}}
    method, path, body, query = opencode.calls[-1]
    assert (method, path, query) == ("POST", "/session/ses_abc/message", None)
    assert body == {
        "messageID": "msgfixture",
        "parts": [{"type": "text", "text": "Fixture transfer"}],
        "system": IMPORT_NOTE,
        "noReply": True,
    }
    for bad in ("", "message", "bad id", 7):
        with pytest.raises(OpenCodeHostError):
            await opencode.send_message(
                "ses_abc", message_id=bad, text="Fixture transfer"
            )
    with pytest.raises(OpenCodeHostError):
        await opencode.send_message("ses_abc", message_id="msgfixture", text="")


async def wait_for_process_exit(pid, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        await asyncio.sleep(0.02)
    return False


PID_SCRIPT = (
    "import os, sys, time; "
    "open(sys.argv[1], 'w').write(str(os.getpid())); "
    "time.sleep(60)"
)


@pytest.mark.asyncio
async def test_writer_guard_fails_closed_and_allows_a_missing_thread(tmp_path):
    groups = FakeGroups(tmp_path)
    groups.codex.threads[THREAD] = []

    async def unreadable(thread_id):
        raise CodexHostError("Codex app-server is unreachable")

    groups.codex.read_thread = unreadable
    with pytest.raises(ValueError, match="Could not check whether the Codex"):
        await _ensure_no_native_writer(
            groups, {"codex": {"id": THREAD, "status": "available"}}, "opencode"
        )

    groups.codex_host = None
    with pytest.raises(ValueError, match="Codex host provider is unavailable"):
        await _ensure_no_native_writer(
            groups, {"codex": {"id": THREAD, "status": "available"}}, "opencode"
        )

    groups = FakeGroups(tmp_path)
    await _ensure_no_native_writer(
        groups, {"codex": {"id": thread_id(99), "status": "available"}}, "opencode"
    )

    groups.opencode.sessions["ses_busy"] = True
    groups.opencode.messages["ses_busy"] = []
    rows = {"opencode": {"id": "ses_busy", "status": "available"}}
    await _ensure_no_native_writer(groups, rows, "codex")

    async def malformed():
        return []

    groups.opencode.statuses = malformed
    with pytest.raises(ValueError, match="status response was malformed"):
        await _ensure_no_native_writer(groups, rows, "codex")

    async def unreadable_status():
        return {"ses_busy": SimpleNamespace(status="")}

    groups.opencode.statuses = unreadable_status
    with pytest.raises(ValueError, match="session status was malformed"):
        await _ensure_no_native_writer(groups, rows, "codex")

    async def unreachable_status():
        raise OpenCodeHostError("OpenCode server unreachable")

    groups.opencode.statuses = unreachable_status
    with pytest.raises(ValueError, match="Could not check whether the OpenCode"):
        await _ensure_no_native_writer(groups, rows, "codex")

    groups.opencode_host = None
    with pytest.raises(ValueError, match="OpenCode host provider is unavailable"):
        await _ensure_no_native_writer(groups, rows, "codex")


@pytest.mark.asyncio
async def test_delivery_timeout_kills_and_reaps_the_child(tmp_path, monkeypatch):
    pid_path = tmp_path / "pid"
    monkeypatch.setattr("kanbeat.session_switch.DELIVERY_TIMEOUT", 0.2)
    code, output = await _one_shot(
        [sys.executable, "-c", PID_SCRIPT, str(pid_path)], tmp_path, None
    )
    assert code is None
    assert "delivery timeout" in output
    pid = int(pid_path.read_text())
    assert await wait_for_process_exit(pid)


@pytest.mark.asyncio
async def test_cancelled_delivery_kills_and_reaps_the_child(tmp_path):
    pid_path = tmp_path / "pid"
    task = asyncio.create_task(
        _one_shot([sys.executable, "-c", PID_SCRIPT, str(pid_path)], tmp_path, None)
    )
    for _ in range(250):
        if pid_path.exists() and pid_path.read_text():
            break
        await asyncio.sleep(0.02)
    assert pid_path.exists()
    pid = int(pid_path.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await wait_for_process_exit(pid)

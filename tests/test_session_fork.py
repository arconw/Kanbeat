import copy

import pytest

from kanbeat.session_fork import fork_session
from kanbeat.session_switch import SessionSwitchRefused, switch_harness
from test_session_switch_review import (
    THREAD,
    FakeGroups,
    installed,
    make_groups,
    start_marker_count,
)

__all__ = ["installed"]


class ForkGroups(FakeGroups):
    def register_session(self, entry):
        self.entries[entry["id"]] = entry

    async def delete_native(self, entry):
        if entry["harness"] == "opencode":
            await self.opencode.delete_session(entry["native_session"])
        else:
            await self.codex.delete_thread(entry["native_session"])


def forking_groups(root):
    groups = make_groups(root)
    forking = ForkGroups.__new__(ForkGroups)
    forking.__dict__.update(groups.__dict__)
    return forking


async def fork(groups, **fields):
    request = {"terminal": "t1", **fields}
    return await fork_session(groups, request, "Fixture session (fork)", "")


@pytest.mark.asyncio
async def test_fork_to_another_harness_creates_a_new_card_and_keeps_the_source(
    tmp_path, installed
):
    groups = forking_groups(tmp_path)
    before = copy.deepcopy(groups.entries["t1"])

    result = await fork(groups, harness="opencode")

    assert groups.entries["t1"] == before
    card = groups.entries[result["terminal"]]
    assert result["terminal"] != "t1"
    assert card["owner"] == "custom"
    assert card["title"] == "Fixture session (fork)"
    assert card["harness"] == "opencode"
    assert card["native_session"] == result["native_session"]
    assert set(card["native_sessions"]) == {"opencode"}
    assert card["status"] == "stopped"
    assert result["delivered"] == 2
    (sent,) = groups.opencode.sent
    assert sent["session_id"] == result["native_session"]
    assert "Codex question" in sent["text"]
    assert "Codex answer" in sent["text"]
    assert start_marker_count(sent["text"]) == 1
    assert groups.codex.turns == []


@pytest.mark.asyncio
async def test_fork_into_the_same_harness_starts_a_separate_session(
    tmp_path, installed
):
    groups = forking_groups(tmp_path)

    result = await fork(groups)

    card = groups.entries[result["terminal"]]
    assert card["harness"] == "codex"
    assert card["native_session"] != THREAD
    assert result["native_session"] in groups.codex.created
    assert result["delivered"] == 2
    (turn,) = groups.codex.turns
    assert turn["thread_id"] == card["native_session"]
    assert "Codex question" in turn["text"]
    assert "Codex answer" in turn["text"]
    assert groups.entries["t1"]["native_session"] == THREAD


@pytest.mark.asyncio
async def test_fork_carries_the_conversation_of_every_harness_the_card_used(
    tmp_path, installed
):
    groups = forking_groups(tmp_path)
    first = await switch_harness(groups, {"terminal": "t1", "harness": "opencode"})
    groups.opencode.add_assistant(
        first["native_session"], "msg_reply_1", "OpenCode answer"
    )
    groups.codex.turns.clear()

    result = await fork(groups, harness="codex")

    card = groups.entries[result["terminal"]]
    assert card["native_session"] != THREAD
    (turn,) = groups.codex.turns
    for text in ("Codex question", "Codex answer", "OpenCode answer"):
        assert text in turn["text"]
    assert start_marker_count(turn["text"]) == 1
    assert groups.entries["t1"]["native_session"] == first["native_session"]
    assert set(groups.entries["t1"]["native_sessions"]) == {"codex", "opencode"}


@pytest.mark.asyncio
async def test_fork_refuses_a_running_session_or_one_without_a_conversation(
    tmp_path, installed
):
    groups = forking_groups(tmp_path)
    groups.entries["t1"]["status"] = "running"
    with pytest.raises(ValueError, match="Stop the session"):
        await fork(groups)
    groups.entries["t1"]["status"] = "stopped"
    groups.entries["t1"]["native_session"] = None
    with pytest.raises(ValueError, match="no native session ID"):
        await fork(groups)
    assert list(groups.entries) == ["t1"]


@pytest.mark.asyncio
async def test_failed_delivery_registers_no_card(tmp_path, installed):
    groups = forking_groups(tmp_path)
    groups.opencode.fail_send_before = 500

    with pytest.raises(ValueError, match="Nothing was forked"):
        await fork(groups, harness="opencode")

    assert list(groups.entries) == ["t1"]
    assert groups.opencode.deleted == groups.opencode.created


@pytest.mark.asyncio
async def test_fork_offers_a_reduction_when_the_conversation_is_too_large(
    tmp_path, installed, monkeypatch
):
    groups = forking_groups(tmp_path)
    monkeypatch.setattr("kanbeat.session_fork.MAX_TRANSFER_BYTES", 390)

    with pytest.raises(SessionSwitchRefused) as raised:
        await fork(groups, harness="opencode")

    assert raised.value.reason == "transfer_too_large"
    assert list(groups.entries) == ["t1"]
    result = await fork(
        groups, harness="opencode", reduction={"kind": "newest", "count": 1}
    )
    assert result["delivered"] == 1


@pytest.mark.asyncio
async def test_the_supervisor_names_and_groups_the_fork_and_registers_the_card(
    tmp_path, monkeypatch
):
    from kanbeat.supervisor import Supervisor

    supervisor = Supervisor(tmp_path)
    groups = supervisor.session_groups
    groups.groups["g1"] = {"name": "Team"}
    supervisor.terminals.entries["t1"] = {
        "id": "t1",
        "owner": "custom",
        "harness": "codex",
        "title": "Work",
        "group": "g1",
        "status": "stopped",
    }
    seen = {}

    async def fake_fork(owner, request, title, group):
        seen.update(title=title, group=group, harness=request.get("harness"))
        owner.register_session({"id": "t2", "owner": "custom", "title": title})
        return {"terminal": "t2"}

    monkeypatch.setattr("kanbeat.session_groups.fork_session", fake_fork)

    result = await groups.dispatch({"op": "session_fork", "terminal": "t1"})

    assert result == {"terminal": "t2"}
    assert seen == {"title": "Work (fork)", "group": "g1", "harness": None}
    assert supervisor.terminals.entries["t2"]["title"] == "Work (fork)"

    await groups.dispatch(
        {"op": "session_fork", "terminal": "t1", "title": "Mine", "group": ""}
    )
    assert seen["title"] == "Mine" and seen["group"] == ""
    with pytest.raises(ValueError, match="Unknown independent session"):
        await groups.dispatch({"op": "session_fork", "terminal": "missing"})

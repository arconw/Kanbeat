import asyncio
from types import SimpleNamespace

import pytest
from textual.widgets import Button, Select

from kanbeat.session_switch import (
    _Delivery,
    _History,
    switch_harness,
)
from kanbeat.session_transfer import ConversationEvent
from kanbeat.ui.app import Confirm, KanbeatApp
from kanbeat.ui.sessions import (
    SWITCH_TIMEOUT_MESSAGE,
    SessionEdit,
    SwitchChoice,
    switch_eligible,
    switch_failure_message,
    switch_locked,
    switch_question,
    switch_result_message,
    switch_tooltip,
)


def entry(identifier="t1", harness="codex", status="stopped", **fields):
    row = {
        "id": identifier,
        "owner": "custom",
        "group": "",
        "title": "Work",
        "harness": harness,
        "status": status,
        "native_session": "native-" + harness,
        "attached": status in ("starting", "running"),
    }
    row.update(fields)
    return row


class FakeRpc:
    def __init__(self, entries, groups=None):
        self.entries = entries
        self.groups = groups or {}
        self.updates = []
        self.switches = []
        self.outcomes = []

    def snapshot(self):
        return {
            "tickets": [],
            "execution": "stopped",
            "custom": {"groups": dict(self.groups)},
            "terminals": {key: dict(value) for key, value in self.entries.items()},
            "notices": [],
        }

    async def __call__(self, message):
        operation = message.get("op")
        if operation == "snapshot":
            return self.snapshot()
        if operation == "session_update":
            self.updates.append(dict(message))
            current = self.entries[message["terminal"]]
            for field in ("title", "group", "native_session"):
                if field in message:
                    current[field] = message[field]
            return self.snapshot()
        if operation == "session_switch_harness":
            self.switches.append(dict(message))
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            current = self.entries[message["terminal"]]
            current["harness"] = message["harness"]
            current["native_session"] = "switched-" + message["harness"]
            return outcome
        raise AssertionError("unexpected operation: " + str(operation))


def refusal(reason, message=None, **details):
    error = RuntimeError(message or "The source session stays active; retry the switch")
    error.details = {"reason": reason, **details}
    return error


async def open_edit(app, pilot, row):
    app.push_screen(SessionEdit(row))
    for _ in range(50):
        await pilot.pause(0.02)
        if isinstance(app.screen, SessionEdit):
            return
    raise AssertionError("Edit Session did not open")


async def settle(app, pilot, screen_type):
    for _ in range(100):
        await pilot.pause(0.02)
        if isinstance(app.screen, screen_type):
            return
    raise AssertionError(f"{screen_type.__name__} did not open: {app.screen!r}")


async def finish(app, pilot):
    for _ in range(100):
        await pilot.pause(0.02)
        if not isinstance(app.screen, (SessionEdit, Confirm)):
            return
    raise AssertionError("the edit dialog did not close")


def test_eligibility_lock_and_tooltip_follow_the_ticket_scope():
    for harness in ("codex", "claude", "opencode", "pi"):
        assert switch_eligible(entry(harness=harness))
        assert switch_eligible(entry(harness=harness, group="g1"))
    assert not switch_eligible(entry(harness="shell"))
    assert not switch_eligible(entry(role="config"))
    assert not switch_eligible(entry(role="hook"))
    assert not switch_eligible(entry(owner="board"))

    for status in ("starting", "running", "stopping", "recovery_blocked"):
        assert switch_locked(entry(status=status))
        assert "before switching its harness" in switch_tooltip(entry(status=status))
    for status in ("stopped", "interrupted"):
        assert not switch_locked(entry(status=status))
    assert "Stop the session" in switch_tooltip(entry(status="running"))
    assert "Stop the session" in switch_tooltip(entry(status="starting"))


def test_confirmation_names_the_target_and_explains_what_transfers():
    question = switch_question(entry(title="Fix [urgent]"), "claude", "Fix [urgent]")
    assert "Switch Fix \\[urgent] to Claude Code?" in question
    assert "including tool output" in question
    assert "Claude Code and its provider" in question
    assert "Permissions and settings are not transferred" in question
    assert "may behave differently" in question
    assert "[" in question and "]" in question


def test_done_message_reports_delivery_and_one_line_of_unsupported_categories():
    message = switch_result_message(
        entry(title="Fix [urgent]"),
        "claude",
        {"delivered": 2, "unsupported": ["images", "thinking", "images"]},
    )
    assert message.startswith("Switched Fix [urgent] to Claude Code;")
    assert "transferred 2 conversation events" in message
    assert message.endswith("Not transferred: images, thinking.")
    assert "\n" not in message
    empty = switch_result_message(entry(), "pi", {"delivered": 0, "unsupported": []})
    assert empty == "Switched Work to Pi; there was no new conversation."


@pytest.mark.asyncio
async def test_selector_is_present_only_for_eligible_sessions_and_locked_running(
    tmp_path, monkeypatch
):
    eligible = entry(status="stopped")
    running = entry(identifier="t2", status="running")
    shell = entry(identifier="t3", harness="shell", status="stopped")
    config = entry(identifier="t4", role="config", status="stopped")
    hook = entry(identifier="t5", role="hook", status="stopped")
    board = entry(identifier="t6", owner="board", status="stopped")
    fake = FakeRpc(
        {row["id"]: row for row in (eligible, running, shell, config, hook, board)}
    )
    app = KanbeatApp(tmp_path)
    monkeypatch.setattr(app, "rpc", fake)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        for row, shown in (
            (eligible, True),
            (running, True),
            (shell, False),
            (config, False),
            (hook, False),
            (board, False),
        ):
            await open_edit(app, pilot, row)
            selector = list(app.screen.query("#session-harness"))
            assert bool(selector) is shown
            if shown:
                control = selector[0]
                assert isinstance(control, Select)
                assert control.value == row["harness"]
                assert control.disabled is (row is running)
                if row is running:
                    assert "Stop the session" in (control.tooltip or "")
                else:
                    assert control.tooltip is None
            app.pop_screen()
            await pilot.pause(0.1)


@pytest.mark.asyncio
async def test_cancel_sends_nothing_and_confirm_switches_without_losing_the_card(
    tmp_path, monkeypatch
):
    row = entry()
    fake = FakeRpc({"t1": row}, groups={"g1": {"name": "Team"}})
    fake.outcomes.append({"delivered": 2, "unsupported": ["thinking"], "changed": True})
    app = KanbeatApp(tmp_path)
    monkeypatch.setattr(app, "rpc", fake)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        await open_edit(app, pilot, row)
        assert str(app.screen.query_one(".section-title").render()) == "EDIT SESSION"
        select = app.screen.query_one("#session-harness", Select)
        select.value = "claude"
        await pilot.click("#session-save")
        await settle(app, pilot, Confirm)
        text = str(app.screen.query_one(".modal-text").render())
        assert "Claude Code" in text and "including tool output" in text
        assert not fake.updates and not fake.switches

        await pilot.click("#cancel")
        await pilot.pause(0.2)
        assert isinstance(app.screen, SessionEdit)
        assert not fake.updates and not fake.switches
        assert row["harness"] == "codex" and row["title"] == "Work"

        await pilot.click("#session-save")
        await settle(app, pilot, Confirm)
        await pilot.click("#yes")
        await finish(app, pilot)
        assert fake.updates == []
        assert fake.switches == [
            {
                "op": "session_switch_harness",
                "terminal": "t1",
                "harness": "claude",
            }
        ]
        assert row["id"] == "t1"
        assert row["title"] == "Work"
        assert row["group"] == ""
        assert row["harness"] == "claude"
        assert any(
            "Switched Work to Claude Code; transferred 2 conversation events."
            " Not transferred: thinking." in notice.message
            for notice in app._notifications
        )


@pytest.mark.asyncio
async def test_title_and_group_are_saved_before_the_switch(tmp_path, monkeypatch):
    row = entry()
    fake = FakeRpc({"t1": row}, groups={"g1": {"name": "Team"}})
    fake.outcomes.append({"delivered": 1, "unsupported": [], "changed": True})
    app = KanbeatApp(tmp_path)
    monkeypatch.setattr(app, "rpc", fake)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        await open_edit(app, pilot, row)
        title = app.screen.query_one("#session-title")
        title.value = "Renamed"
        app.screen.query_one("#session-group", Select).value = "g1"
        app.screen.query_one("#session-harness", Select).value = "pi"
        await pilot.click("#session-save")
        await settle(app, pilot, Confirm)
        await pilot.click("#yes")
        await finish(app, pilot)
        assert fake.updates == [
            {
                "op": "session_update",
                "terminal": "t1",
                "title": "Renamed",
                "group": "g1",
                "native_session": "native-codex",
            }
        ]
        assert fake.switches == [
            {
                "op": "session_switch_harness",
                "terminal": "t1",
                "harness": "pi",
            }
        ]
        assert row["id"] == "t1"
        assert row["title"] == "Renamed"
        assert row["group"] == "g1"
        assert row["harness"] == "pi"


@pytest.mark.asyncio
async def test_a_large_conversation_offers_a_reduction_or_cancel(tmp_path, monkeypatch):
    row = entry()
    fake = FakeRpc({"t1": row})
    fake.outcomes.append(
        refusal(
            "transfer_too_large",
            message=(
                "The planned conversation is 1200000 bytes, above the 1048576-byte "
                "transfer limit. Nothing was truncated."
            ),
            options=[
                {
                    "kind": "newest",
                    "count": 20,
                    "description": "Newest 20 events",
                    "size": {"characters": 4000, "bytes": 12000},
                },
                {
                    "kind": "text_only",
                    "count": None,
                    "description": "User and assistant text only",
                    "size": {"characters": 1000, "bytes": 3000},
                },
            ],
            unsupported=["images"],
        )
    )
    fake.outcomes.append({"delivered": 20, "unsupported": [], "changed": True})
    app = KanbeatApp(tmp_path)
    monkeypatch.setattr(app, "rpc", fake)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        await open_edit(app, pilot, row)
        app.screen.query_one("#session-harness", Select).value = "claude"
        await pilot.click("#session-save")
        await settle(app, pilot, Confirm)
        await pilot.click("#yes")
        await settle(app, pilot, SwitchChoice)
        text = str(app.screen.query_one(".modal-text").render())
        assert (
            "above the" in text and "Not transferred from the source: images." in text
        )
        labels = [str(button.label) for button in app.screen.query(".choice")]
        assert len(labels) == 2
        assert "Newest 20 events (12 KB)" in labels[0]
        assert "User and assistant text only (3 KB)" in labels[1]

        await pilot.click("#switch-choice-0")
        await finish(app, pilot)
        assert fake.switches[1] == {
            "op": "session_switch_harness",
            "terminal": "t1",
            "harness": "claude",
            "reduction": {"kind": "newest", "count": 20},
        }
        assert row["harness"] == "claude"


@pytest.mark.asyncio
async def test_cancelling_a_large_conversation_changes_nothing(tmp_path, monkeypatch):
    row = entry()
    fake = FakeRpc({"t1": row})
    fake.outcomes.append(
        refusal(
            "transfer_too_large",
            message=(
                "The planned conversation is 1200000 bytes, above the 1048576-byte "
                "transfer limit. Nothing was truncated."
            ),
            options=[
                {
                    "kind": "text_only",
                    "count": None,
                    "description": "User and assistant text only",
                    "size": {"characters": 10, "bytes": 10},
                }
            ],
        )
    )
    app = KanbeatApp(tmp_path)
    monkeypatch.setattr(app, "rpc", fake)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        await open_edit(app, pilot, row)
        app.screen.query_one("#session-harness", Select).value = "claude"
        await pilot.click("#session-save")
        await settle(app, pilot, Confirm)
        await pilot.click("#yes")
        await settle(app, pilot, SwitchChoice)
        await pilot.click("#cancel")
        await pilot.pause(0.2)
        assert isinstance(app.screen, SessionEdit)
        assert row["harness"] == "codex"
        assert len(fake.switches) == 1


@pytest.mark.asyncio
async def test_a_failure_keeps_the_dialog_open_for_retry(tmp_path, monkeypatch):
    row = entry()
    fake = FakeRpc({"t1": row})
    fake.outcomes.append(
        RuntimeError(
            "Delivery to claude was not confirmed. The source codex session "
            "stays active. Retry the switch"
        )
    )
    fake.outcomes.append({"delivered": 1, "unsupported": [], "changed": True})
    app = KanbeatApp(tmp_path)
    monkeypatch.setattr(app, "rpc", fake)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        await open_edit(app, pilot, row)
        app.screen.query_one("#session-harness", Select).value = "claude"
        await pilot.click("#session-save")
        await settle(app, pilot, Confirm)
        await pilot.click("#yes")
        for _ in range(100):
            await pilot.pause(0.02)
            if any(
                "Retry the switch" in notice.message for notice in app._notifications
            ):
                break
        assert isinstance(app.screen, SessionEdit)
        assert row["harness"] == "codex"
        assert any(
            notice.severity == "error" and "Retry the switch" in notice.message
            for notice in app._notifications
        )

        await pilot.click("#session-save")
        await settle(app, pilot, Confirm)
        await pilot.click("#yes")
        await finish(app, pilot)
        assert row["harness"] == "claude"
        assert len(fake.switches) == 2


@pytest.mark.asyncio
async def test_missing_target_offers_a_fresh_session_and_cancel_is_safe(
    tmp_path, monkeypatch
):
    row = entry()
    fake = FakeRpc({"t1": row})
    fake.outcomes.append(refusal("target_session_missing", target="claude"))
    fake.outcomes.append({"delivered": 0, "unsupported": [], "changed": True})
    app = KanbeatApp(tmp_path)
    monkeypatch.setattr(app, "rpc", fake)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        await open_edit(app, pilot, row)
        app.screen.query_one("#session-harness", Select).value = "claude"
        await pilot.click("#session-save")
        await settle(app, pilot, Confirm)
        await pilot.click("#yes")
        await settle(app, pilot, SwitchChoice)
        assert (
            str(app.screen.query_one(".modal-name").render())
            == "TARGET SESSION MISSING"
        )
        assert "Start a fresh Claude Code session" in str(
            app.screen.query(".choice")[0].label
        )
        await pilot.click("#switch-choice-0")
        await finish(app, pilot)
        assert fake.switches[1]["fresh_session"] is True


class StubGroups:
    def __init__(self, root, entry):
        self.state = SimpleNamespace(
            board=SimpleNamespace(root=root / ".kanbeat", project=root),
            save=lambda: None,
        )
        self.entry = entry
        self.messages = []
        self.codex_host = None
        self.opencode_host = None

    def custom(self, terminal_id):
        assert terminal_id == self.entry["id"]
        return self.entry

    def inactive(self, entry):
        assert entry["status"] not in ("starting", "running", "stopping")

    def progress(self, message, level="information"):
        self.messages.append((message, level))


@pytest.mark.asyncio
async def test_the_switch_reports_its_stages_before_the_done_notice(
    tmp_path, monkeypatch
):
    target = entry(harness="claude")
    target["native_sessions"] = {
        "claude": {"id": "native-claude", "status": "available"},
        "pi": {"id": "native-pi", "status": "available"},
    }
    groups = StubGroups(tmp_path, target)
    event = ConversationEvent(
        harness="claude", ordinal=0, role="user", text="Hi", event_id="event-1"
    )
    read = _History(
        harness="claude",
        outcome="available",
        events=(event,),
        complete=True,
    )

    async def read_history(groups, harness, session_id, ledger):
        return read

    async def present(groups, harness, session_id):
        return True

    async def deliver(groups, harness, session_id, plan, context_path, created):
        return _Delivery("target-message")

    monkeypatch.setattr("kanbeat.session_switch.available", lambda name: True)
    monkeypatch.setattr("kanbeat.session_switch._read_history", read_history)
    monkeypatch.setattr("kanbeat.session_switch._target_present", present)
    monkeypatch.setattr("kanbeat.session_switch._reconcile_target", noop)
    monkeypatch.setattr("kanbeat.session_switch._deliver", deliver)

    result = await switch_harness(groups, {"terminal": "t1", "harness": "pi"})
    assert [message for message, _ in groups.messages] == [
        "Reading conversation history",
        "Preparing the target session",
        "Delivering the conversation to Pi",
    ]
    assert result["delivered"] == 1


async def noop(*args, **kwargs):
    return None


def test_progress_is_forwarded_to_the_existing_notice_channel(tmp_path):
    from kanbeat.session_groups import SessionGroups

    notices = []
    groups = SessionGroups(
        SimpleNamespace(
            board=SimpleNamespace(root=tmp_path / ".kanbeat", project=tmp_path),
            data={},
            save=lambda: None,
        ),
        SimpleNamespace(entries={}, sessions={}),
        notice=lambda message, level: notices.append((message, level)),
    )
    groups.progress("Reading conversation history")
    groups.progress("Delivery failed", "error")
    assert notices == [
        ("Reading conversation history", "information"),
        ("Delivery failed", "error"),
    ]


@pytest.mark.asyncio
async def test_choosing_the_current_harness_sends_nothing(tmp_path, monkeypatch):
    row = entry()
    fake = FakeRpc({"t1": row})
    app = KanbeatApp(tmp_path)
    monkeypatch.setattr(app, "rpc", fake)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        await open_edit(app, pilot, row)
        await pilot.click("#session-save")
        await finish(app, pilot)
        assert not fake.updates and not fake.switches
        assert row["harness"] == "codex"


@pytest.mark.asyncio
async def test_only_the_switch_rpc_gets_the_longer_timeout(tmp_path, monkeypatch):
    calls = []

    async def fake_request(project, message, timeout=10):
        calls.append((dict(message), timeout))
        return {}

    monkeypatch.setattr("kanbeat.ui.app.request", fake_request)
    app = KanbeatApp(tmp_path)
    await app.rpc({"op": "snapshot"})
    await app.rpc({"op": "session_switch_harness", "terminal": "t1", "harness": "pi"})
    assert [timeout for _, timeout in calls] == [10.0, 360.0]


class BlockingRpc:
    def __init__(self, row):
        self.entry = row
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.cancelled = False
        self.finished = False
        self.switches = []

    def snapshot(self):
        return {
            "tickets": [],
            "execution": "stopped",
            "custom": {"groups": {}},
            "terminals": {self.entry["id"]: dict(self.entry)},
            "notices": [],
        }

    async def __call__(self, message):
        operation = message.get("op")
        if operation == "snapshot":
            return self.snapshot()
        if operation == "session_switch_harness":
            self.switches.append(dict(message))
            self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            self.finished = True
            self.entry["harness"] = message["harness"]
            self.entry["native_session"] = "switched-" + message["harness"]
            return {"delivered": 1, "unsupported": [], "changed": True}
        raise AssertionError("unexpected operation: " + str(operation))


@pytest.mark.asyncio
async def test_an_in_flight_switch_cannot_be_dismissed(tmp_path, monkeypatch):
    row = entry()
    fake = BlockingRpc(row)
    app = KanbeatApp(tmp_path)
    monkeypatch.setattr(app, "rpc", fake)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        await open_edit(app, pilot, row)
        app.screen.query_one("#session-harness", Select).value = "claude"
        await pilot.click("#session-save")
        await settle(app, pilot, Confirm)
        await pilot.click("#yes")
        await asyncio.wait_for(fake.started.wait(), 2)
        assert isinstance(app.screen, SessionEdit)
        cancel = app.screen.query_one("#cancel", Button)
        assert cancel.disabled
        await pilot.click("#cancel")
        await pilot.pause(0.1)
        assert isinstance(app.screen, SessionEdit)
        app.screen.dismiss(None)
        await pilot.pause(0.1)
        assert isinstance(app.screen, SessionEdit)
        assert any(
            "Wait for the harness switch to finish." in notice.message
            for notice in app._notifications
        )
        assert not fake.cancelled and len(fake.switches) == 1

        fake.release.set()
        await finish(app, pilot)
        assert fake.finished
        assert row["harness"] == "claude"
        assert any(
            "Switched Work to Claude Code; transferred 1 conversation event."
            in notice.message
            for notice in app._notifications
        )


@pytest.mark.asyncio
async def test_a_timed_out_switch_keeps_the_dialog_open_with_unknown_outcome(
    tmp_path, monkeypatch
):
    row = entry()
    fake = FakeRpc({"t1": row})
    fake.outcomes.append(TimeoutError())
    app = KanbeatApp(tmp_path)
    monkeypatch.setattr(app, "rpc", fake)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        await open_edit(app, pilot, row)
        app.screen.query_one("#session-harness", Select).value = "claude"
        await pilot.click("#session-save")
        await settle(app, pilot, Confirm)
        await pilot.click("#yes")
        for _ in range(100):
            await pilot.pause(0.02)
            if any(
                notice.message == SWITCH_TIMEOUT_MESSAGE
                for notice in app._notifications
            ):
                break
        assert isinstance(app.screen, SessionEdit)
        assert row["harness"] == "codex"
        assert any(
            notice.severity == "error" and notice.message == SWITCH_TIMEOUT_MESSAGE
            for notice in app._notifications
        )
        assert len(fake.switches) == 1
        assert app.screen.query_one("#cancel", Button).disabled is False


def test_switch_failure_message_drops_protocol_detail_but_keeps_the_retry_path():
    message = switch_failure_message(
        RuntimeError(
            "Delivery to claude was not confirmed: raw protocol detail. "
            "The source codex session stays active. Retry the switch"
        )
    )
    assert message == (
        "Delivery to claude was not confirmed. "
        "The source codex session stays active. Retry the switch"
    )
    collision = switch_failure_message(
        RuntimeError("The planned conversation is too large: 2 MB")
    )
    assert collision == "The planned conversation is too large: 2 MB"
    assert switch_failure_message(RuntimeError("line one\nline  two")) == (
        "line one line two"
    )

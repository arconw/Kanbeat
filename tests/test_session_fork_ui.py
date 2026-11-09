import pytest
from textual.containers import VerticalScroll
from textual.widgets import Button, Select

from kanbeat.ui.app import Assignment, Confirm, KanbeatApp
from kanbeat.ui.sessions import (
    SessionActions,
    SessionEdit,
    SessionFork,
    fork_blocker,
    fork_eligible,
    fork_question,
    fork_result_message,
    fork_title,
)
from test_session_edit_switch_review import FakeRpc, entry, settle


class ForkRpc(FakeRpc):
    def __init__(self, entries, groups=None):
        super().__init__(entries, groups)
        self.forks = []

    async def __call__(self, message):
        if message.get("op") != "session_fork":
            return await super().__call__(message)
        self.forks.append(dict(message))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


async def open_fork(app, pilot, row):
    app.push_screen(SessionFork(row))
    await settle(app, pilot, SessionFork)


async def closed(app, pilot, screen_type):
    for _ in range(100):
        await pilot.pause(0.02)
        if not isinstance(app.screen, screen_type):
            return
    raise AssertionError(f"{screen_type.__name__} did not close")


def test_fork_follows_the_switch_eligibility_and_needs_a_stopped_conversation():
    assert fork_eligible(entry())
    assert not fork_eligible(entry(harness="shell"))
    assert not fork_eligible(entry(role="config"))
    assert fork_blocker(entry()) == ""
    assert "Stop the session" in fork_blocker(entry(status="running"))
    assert "no conversation" in fork_blocker(entry(native_session=None))


def test_fork_copy_fits_the_name_limit_and_names_the_target():
    assert fork_title("Work") == "Work (fork)"
    assert len(fork_title("x" * 200)) == 64
    assert fork_title("x" * 200).endswith(" (fork)")
    question = fork_question(entry(title="Fix [urgent]"), "claude", "Copy")
    assert "Fork Fix \\[urgent] to Claude Code as Copy?" in question
    assert "Claude Code and its provider" in question
    assert "original session is not changed" in question
    message = fork_result_message({"delivered": 2, "unsupported": ["images"]}, "Copy")
    assert message == (
        "Forked Copy; transferred 2 conversation events. Not transferred: images."
    )
    assert fork_result_message({"delivered": 0}, "Copy").endswith(
        "no conversation to transfer."
    )


@pytest.mark.asyncio
async def test_fork_defaults_to_the_source_harness_and_skips_the_confirmation(
    tmp_path, monkeypatch
):
    row = entry(group="g1")
    fake = ForkRpc({"t1": row}, groups={"g1": {"name": "Team"}})
    fake.outcomes.append({"delivered": 2, "unsupported": [], "terminal": "t2"})
    app = KanbeatApp(tmp_path)
    monkeypatch.setattr(app, "rpc", fake)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        await open_fork(app, pilot, row)
        assert app.screen.query_one("#fork-harness", Select).value == "codex"
        assert app.screen.query_one("#fork-group", Select).value == "g1"
        assert app.screen.query_one("#fork-title").value == "Work (fork)"
        await pilot.click("#fork-start")
        await closed(app, pilot, SessionFork)
        assert fake.forks == [
            {
                "op": "session_fork",
                "terminal": "t1",
                "title": "Work (fork)",
                "group": "g1",
                "harness": "codex",
            }
        ]
        assert any(
            "Forked Work (fork); transferred 2 conversation events." in n.message
            for n in app._notifications
        )


@pytest.mark.asyncio
async def test_forking_to_another_harness_asks_first_and_cancel_sends_nothing(
    tmp_path, monkeypatch
):
    row = entry()
    fake = ForkRpc({"t1": row})
    fake.outcomes.append({"delivered": 1, "unsupported": []})
    app = KanbeatApp(tmp_path)
    monkeypatch.setattr(app, "rpc", fake)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        await open_fork(app, pilot, row)
        app.screen.query_one("#fork-harness", Select).value = "claude"
        await pilot.click("#fork-start")
        await settle(app, pilot, Confirm)
        await pilot.click("#cancel")
        await settle(app, pilot, SessionFork)
        assert fake.forks == []
        await pilot.click("#fork-start")
        await settle(app, pilot, Confirm)
        await pilot.click("#yes")
        await closed(app, pilot, SessionFork)
        assert [fork["harness"] for fork in fake.forks] == ["claude"]


@pytest.mark.asyncio
async def test_a_failed_fork_keeps_the_form_open_and_reports_the_reason(
    tmp_path, monkeypatch
):
    row = entry()
    fake = ForkRpc({"t1": row})
    fake.outcomes.append(RuntimeError("Nothing was forked; retry"))
    app = KanbeatApp(tmp_path)
    monkeypatch.setattr(app, "rpc", fake)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        await open_fork(app, pilot, row)
        await pilot.click("#fork-start")
        await pilot.pause(0.3)
        assert isinstance(app.screen, SessionFork)
        assert any("Nothing was forked" in n.message for n in app._notifications)
        assert not app.screen.query_one("#fork-start", Button).disabled


@pytest.mark.asyncio
async def test_the_menu_offers_fork_and_locks_it_while_the_session_runs(
    tmp_path, monkeypatch
):
    app = KanbeatApp(tmp_path)
    monkeypatch.setattr(app, "rpc", ForkRpc({}))
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        app.push_screen(SessionActions(entry(status="running")))
        await settle(app, pilot, SessionActions)
        locked = app.screen.query_one("#session-fork", Button)
        assert locked.disabled
        assert "Stop the session" in str(locked.tooltip)
        app.pop_screen()
        await pilot.pause()
        app.push_screen(SessionActions(entry()))
        await settle(app, pilot, SessionActions)
        assert not app.screen.query_one("#session-fork", Button).disabled
        await pilot.click("#session-fork")
        await settle(app, pilot, SessionFork)
        app.pop_screen()
        await pilot.pause()
        app.push_screen(SessionActions(entry(harness="shell")))
        await settle(app, pilot, SessionActions)
        assert not app.screen.query("#session-fork")


@pytest.mark.asyncio
@pytest.mark.parametrize("make", ["edit", "fork", "assignment"])
async def test_every_form_scrolls_to_its_last_button_in_a_small_terminal(
    tmp_path, monkeypatch, make
):
    row = entry()
    screens = {
        "edit": lambda: SessionEdit(row),
        "fork": lambda: SessionFork(row),
        "assignment": lambda: Assignment(
            {"id": "001", "title": "T", "state": {"developer": {}}}, "developer"
        ),
    }
    app = KanbeatApp(tmp_path)
    monkeypatch.setattr(app, "rpc", ForkRpc({"t1": row}))
    async with app.run_test(size=(100, 14)) as pilot:
        await pilot.pause(0.3)
        screen = screens[make]()
        app.push_screen(screen)
        await settle(app, pilot, type(screen))
        box = screen.query(".sm").first()
        assert box.max_scroll_y > 0
        box.scroll_end(animate=False)
        await pilot.pause(0.1)
        last = list(screen.query(".modal-actions Button"))[-1]
        assert box.region.contains_region(last.region)
        assert isinstance(box, VerticalScroll) or box.styles.overflow_y == "auto"

import asyncio

import pytest
from textual.widgets import Button, Collapsible, Static

from kanbeat.supervisor import Supervisor
from kanbeat.ui.app import KanbeatApp


def make_app(tmp_path, monkeypatch, changes):
    supervisor = Supervisor(tmp_path)
    for number in range(1, changes + 1):
        supervisor.runtime.change_config({"global_cp": f"v{number}"}, "cli")
    app = KanbeatApp(tmp_path)
    monkeypatch.setattr(app, "rpc", supervisor.dispatch)
    return supervisor, app


async def until(pilot, condition, seconds=3):
    for _ in range(int(seconds / 0.02)):
        if condition():
            return
        await pilot.pause(0.02)
    assert condition()


async def click_button(pilot, app, selector):
    await until(pilot, lambda: not app.query_one(selector).has_class("-active"))
    app.query_one(selector).scroll_visible(animate=False, immediate=True)
    await pilot.pause()
    await pilot.click(selector)


def entries(app):
    return [item.id for item in app.query("#cfg-history Collapsible")]


@pytest.mark.asyncio
async def test_history_shows_five_collapsed_entries_and_more_on_request(
    tmp_path, monkeypatch
):
    supervisor, app = make_app(tmp_path, monkeypatch, 7)
    async with app.run_test(size=(140, 70)) as pilot:
        await pilot.pause()
        await pilot.click("#nav-settings")
        await until(pilot, lambda: len(entries(app)) == 5)
        assert entries(app) == [f"cfg-entry-{n}" for n in (7, 6, 5, 4, 3)]
        assert all(item.collapsed for item in app.query("#cfg-history Collapsible"))
        more = app.query_one("#cfg-history-more", Button)
        assert more.display and "5 of 7" in str(more.label)
        first = app.query_one("#cfg-entry-7", Collapsible)
        assert "global_cp" in first.title and "cli" in first.title
        first.collapsed = False
        await pilot.pause()
        detail = first.query_one(".history-detail", Static)
        assert "was: v6" in str(detail.render()) and "now: v7" in str(detail.render())
        await click_button(pilot, app, "#cfg-history-more")
        await until(pilot, lambda: len(entries(app)) == 7)
        assert not app.query_one("#cfg-history-more", Button).display
        assert not app.query_one("#cfg-entry-7", Collapsible).collapsed


@pytest.mark.asyncio
async def test_revert_undoes_a_change_and_the_list_follows(tmp_path, monkeypatch):
    supervisor, app = make_app(tmp_path, monkeypatch, 2)
    async with app.run_test(size=(140, 70)) as pilot:
        await pilot.pause()
        await pilot.click("#nav-settings")
        await until(pilot, lambda: len(entries(app)) == 2)
        app.query_one("#cfg-entry-2", Collapsible).collapsed = False
        await pilot.pause()
        await click_button(pilot, app, "#cfg-revert-2")
        await until(pilot, lambda: "cfg-entry-3" in entries(app))
        assert supervisor.runtime.config["global_cp"] == "v1"
        assert supervisor.runtime.config_history()[-1]["source"] == "tui revert"
        assert entries(app)[0] == "cfg-entry-3"
        assert app.query_one("#cfg-global_cp").text == "v1"


@pytest.mark.asyncio
async def test_a_change_from_the_command_line_appears_without_reopening(
    tmp_path, monkeypatch
):
    supervisor, app = make_app(tmp_path, monkeypatch, 1)
    async with app.run_test(size=(140, 70)) as pilot:
        await pilot.pause()
        await pilot.click("#nav-settings")
        await until(pilot, lambda: len(entries(app)) == 1)
        await supervisor.dispatch(
            {
                "op": "config_set",
                "changes": {"developer_prompt": "From an agent"},
                "source": "cli",
            }
        )
        await app.refresh_snapshot(force=True)
        await until(pilot, lambda: len(entries(app)) == 2)
        assert entries(app)[0] == "cfg-entry-2"
        assert app.query_one("#cfg-developer_prompt").text == "From an agent"
        assert any("Config changed" in n.message for n in app._notifications)


@pytest.mark.asyncio
async def test_history_can_be_loaded_up_to_a_hundred_entries(tmp_path, monkeypatch):
    supervisor, app = make_app(tmp_path, monkeypatch, 130)
    async with app.run_test(size=(140, 70)) as pilot:
        await pilot.pause()
        await pilot.click("#nav-settings")
        await until(pilot, lambda: len(entries(app)) == 5)
        for expected in (25, 45, 65, 85, 100):
            await click_button(pilot, app, "#cfg-history-more")
            await until(pilot, lambda: len(entries(app)) == expected, seconds=20)
        await until(
            pilot, lambda: not app.query_one("#cfg-history-more", Button).display
        )
        assert entries(app)[0] == "cfg-entry-130" and entries(app)[-1] == "cfg-entry-31"


@pytest.mark.asyncio
async def test_overlapping_history_loads_do_not_clash(tmp_path, monkeypatch):
    supervisor, app = make_app(tmp_path, monkeypatch, 6)
    async with app.run_test(size=(140, 70)) as pilot:
        await pilot.pause()
        await pilot.click("#nav-settings")
        await until(pilot, lambda: len(entries(app)) == 5)
        settings = app.query_one("#settings")
        settings.history_limit = 25
        await asyncio.gather(*(settings.load_history() for _ in range(4)))
        assert len(entries(app)) == 6
        assert len(set(entries(app))) == 6

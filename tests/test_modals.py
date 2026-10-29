import pytest
from textual.widgets import Input, Select

from kanbeat.supervisor import Supervisor
from kanbeat.ui.app import Confirm, KanbeatApp, NewTicket
from kanbeat.ui.sessions import GroupForm, NewSession


@pytest.fixture
def app(tmp_path, monkeypatch):
    supervisor = Supervisor(tmp_path)
    app = KanbeatApp(tmp_path)
    monkeypatch.setattr(app, "rpc", supervisor.dispatch)
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("dialog", [NewTicket, GroupForm, NewSession])
async def test_backdrop_discards_forms_without_mutation(app, dialog):
    async with app.run_test(size=(140, 60)) as pilot:
        original = app.snapshot.copy()
        screen = dialog()
        app.push_screen(screen)
        await pilot.pause()
        screen.query(Input).first().value = "Unsaved"
        await pilot.click(offset=(0, 0))
        await pilot.pause()
        assert app.screen is not screen
        assert app.snapshot == original


@pytest.mark.asyncio
async def test_inside_click_and_drag_do_not_dismiss(app):
    async with app.run_test(size=(140, 60)) as pilot:
        screen = GroupForm()
        app.push_screen(screen)
        await pilot.pause()
        await pilot.click("#group-name")
        assert app.screen is screen
        await pilot.mouse_down("#group-name")
        await pilot.hover(offset=(0, 0))
        await pilot.mouse_up(offset=(0, 0))
        await pilot.pause()
        assert app.screen is screen


@pytest.mark.asyncio
async def test_backdrop_cancels_only_top_dialog_and_consumes_click(app):
    results = []
    async with app.run_test(size=(140, 60)) as pilot:
        form = GroupForm()
        app.push_screen(form)
        await pilot.pause()
        confirm = Confirm("Delete?")
        app.push_screen(confirm, results.append)
        await pilot.pause()
        await pilot.click(offset=(0, 0))
        await pilot.pause()
        assert app.screen is form
        assert results == [False]
        assert not form.query_one("#group-name", Input).value


@pytest.mark.asyncio
async def test_dropdown_interaction_stays_inside_dialog(app):
    async with app.run_test(size=(140, 60)) as pilot:
        screen = NewSession()
        app.push_screen(screen)
        await pilot.pause()
        await pilot.click("#session-harness")
        await pilot.press("down", "enter")
        assert app.screen is screen
        assert screen.query_one("#session-harness", Select).value == "claude"

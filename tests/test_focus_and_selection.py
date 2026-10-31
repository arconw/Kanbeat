import pytest
from textual.widgets import Button, Label, Markdown, Static

from kanbeat.supervisor import Supervisor
from kanbeat.ui.app import KanbeatApp, SelectableText


@pytest.mark.asyncio
async def test_a_focused_button_is_not_drawn_in_reverse(tmp_path, monkeypatch):
    supervisor = Supervisor(tmp_path)
    supervisor.board.create("A task")
    app = KanbeatApp(tmp_path)
    monkeypatch.setattr(app, "rpc", supervisor.dispatch)
    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.pause()
        buttons = [
            app.query_one("#nav-board", Button),
            app.query_one("#new-group", Button),
        ]
        for button in buttons:
            button.focus()
            await pilot.pause()
            assert button.has_focus
            assert not button.rich_style.reverse
            assert button.rich_style.bold


def test_plain_labels_are_not_selectable_but_text_you_read_or_edit_is():
    assert not Static.ALLOW_SELECT and not Label.ALLOW_SELECT
    assert SelectableText.ALLOW_SELECT and Markdown.ALLOW_SELECT

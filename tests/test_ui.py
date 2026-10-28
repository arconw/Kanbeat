import pytest
from textual.widgets import Button, Select, TextArea

from kanbeat.supervisor import Supervisor
from kanbeat.ui.app import KanbeatApp


@pytest.mark.asyncio
async def test_settings_save_transition_hook(tmp_path, monkeypatch):
    supervisor = Supervisor(tmp_path)
    app = KanbeatApp(tmp_path)
    monkeypatch.setattr(app, "rpc", supervisor.dispatch)
    async with app.run_test(size=(140, 60)) as pilot:
        await pilot.click("#nav-settings")
        await pilot.pause()
        app.query_one("#cfg-hook-harness", Select).value = "claude"
        app.query_one("#cfg-hook-prompt", TextArea).load_text("Update linked issue")
        app.query_one("#settings-save", Button).press()
        await pilot.pause()
        assert supervisor.runtime.config["transition_hook"] == {
            "harness": "claude", "prompt": "Update linked issue",
        }
        assert Supervisor(tmp_path).runtime.config["transition_hook"] == {
            "harness": "claude", "prompt": "Update linked issue",
        }

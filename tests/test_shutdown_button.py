import pytest
from textual.widgets import Button

from kanbeat.ui import app as app_module
from kanbeat.ui.app import Confirm, KanbeatApp, NavMenu
from test_session_edit_switch_review import FakeRpc, settle


class ShutdownRpc(FakeRpc):
    def __init__(self):
        super().__init__({})
        self.shutdowns = []
        self.failure = None

    async def __call__(self, message):
        if message.get("op") != "shutdown":
            return await super().__call__(message)
        self.shutdowns.append(dict(message))
        if self.failure is not None:
            raise self.failure
        return {"shutdown": "requested"}


@pytest.fixture
def gone(monkeypatch):
    class Missing:
        @staticmethod
        def exists():
            return False

    monkeypatch.setattr(app_module, "socket_path", lambda project: Missing())


@pytest.mark.asyncio
async def test_shutdown_sits_between_play_and_quit_and_asks_first(
    tmp_path, monkeypatch, gone
):
    fake = ShutdownRpc()
    app = KanbeatApp(tmp_path)
    monkeypatch.setattr(app, "rpc", fake)
    exits = []
    monkeypatch.setattr(app, "exit", lambda *args, **kwargs: exits.append(True))
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        play = app.query_one("#execution", Button)
        shutdown = app.query_one("#shutdown", Button)
        quit_button = app.query_one("#quit", Button)
        assert str(shutdown.label) == "Shutdown"
        assert play.region.right <= shutdown.region.x < quit_button.region.x

        await pilot.click("#shutdown")
        await settle(app, pilot, Confirm)
        await pilot.click("#cancel")
        await pilot.pause(0.2)
        assert fake.shutdowns == [] and exits == []

        await pilot.click("#shutdown")
        await settle(app, pilot, Confirm)
        await pilot.click("#yes")
        await pilot.pause(0.3)
        assert fake.shutdowns == [{"op": "shutdown", "confirmed": True}]
        assert exits == [True]


@pytest.mark.asyncio
async def test_a_refused_shutdown_keeps_the_tui_open_and_reports_why(
    tmp_path, monkeypatch, gone
):
    fake = ShutdownRpc()
    fake.failure = RuntimeError("Shutdown refused")
    app = KanbeatApp(tmp_path)
    monkeypatch.setattr(app, "rpc", fake)
    exits = []
    monkeypatch.setattr(app, "exit", lambda *args, **kwargs: exits.append(True))
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        await pilot.click("#shutdown")
        await settle(app, pilot, Confirm)
        await pilot.click("#yes")
        await pilot.pause(0.3)
        assert exits == []
        assert any("Shutdown refused" in n.message for n in app._notifications)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "width,embedded,collapsed",
    [
        (99, False, True),
        (107, False, True),
        (108, False, False),
        (114, True, True),
        (124, True, True),
        (125, True, False),
        (146, True, False),
        (140, False, False),
    ],
)
async def test_the_right_hand_buttons_fold_into_a_burger_when_they_do_not_fit(
    tmp_path, monkeypatch, width, embedded, collapsed
):
    app = KanbeatApp(tmp_path)
    monkeypatch.setattr(app, "rpc", ShutdownRpc())
    async with app.run_test(size=(width, 30)) as pilot:
        await pilot.pause(0.3)
        app.embedded_terminal = "terminal" if embedded else None
        app.update_diff_button()
        await pilot.pause(0.1)
        burger = app.query_one("#nav-menu", Button)
        assert burger.display is collapsed
        for name in ("#nav-sessions", "#execution", "#shutdown", "#quit"):
            assert app.query_one(name).display is not collapsed
        if collapsed:
            assert burger.region.right <= width
        else:
            assert app.query_one("#quit", Button).region.right <= width


@pytest.mark.asyncio
async def test_the_burger_menu_reaches_every_collapsed_button(
    tmp_path, monkeypatch, gone
):
    fake = ShutdownRpc()
    app = KanbeatApp(tmp_path)
    monkeypatch.setattr(app, "rpc", fake)
    exits = []
    monkeypatch.setattr(app, "exit", lambda *args, **kwargs: exits.append(True))
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause(0.3)
        await pilot.click("#nav-menu")
        await settle(app, pilot, NavMenu)
        ids = [button.id for button in app.screen.query(Button)]
        assert ids == [
            "nav-sessions",
            "nav-board",
            "nav-archive",
            "nav-settings",
            "execution",
            "shutdown",
            "quit",
        ]
        assert app.screen.query_one("#nav-sessions", Button).disabled

        await pilot.click("#nav-board")
        await pilot.pause(0.3)
        assert app.tab == "board"
        assert not isinstance(app.screen, NavMenu)

        await pilot.click("#nav-menu")
        await settle(app, pilot, NavMenu)
        await pilot.click("#shutdown")
        await settle(app, pilot, Confirm)
        await pilot.click("#yes")
        await pilot.pause(0.3)
        assert fake.shutdowns == [{"op": "shutdown", "confirmed": True}]
        assert exits == [True]


@pytest.mark.asyncio
async def test_the_burger_menu_starts_the_board_and_quits(tmp_path, monkeypatch):
    fake = ShutdownRpc()
    played = []
    original = fake.__call__

    async def call(message):
        if message.get("op") in ("play", "stop"):
            played.append(message["op"])
            return {}
        return await original(message)

    app = KanbeatApp(tmp_path)
    monkeypatch.setattr(app, "rpc", call)
    exits = []
    monkeypatch.setattr(app, "exit", lambda *args, **kwargs: exits.append(True))
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause(0.3)
        await pilot.click("#nav-menu")
        await settle(app, pilot, NavMenu)
        await pilot.click("#execution")
        await pilot.pause(0.3)
        assert played == ["play"]
        await pilot.click("#nav-menu")
        await settle(app, pilot, NavMenu)
        await pilot.click("#quit")
        await pilot.pause(0.3)
        assert exits == [True]


@pytest.mark.asyncio
async def test_clicking_outside_the_burger_menu_closes_it_and_does_nothing(
    tmp_path, monkeypatch
):
    app = KanbeatApp(tmp_path)
    monkeypatch.setattr(app, "rpc", ShutdownRpc())
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause(0.3)
        tab = app.tab
        await pilot.click("#nav-menu")
        await settle(app, pilot, NavMenu)
        await pilot.click(offset=(5, 20))
        await pilot.pause(0.3)
        assert not isinstance(app.screen, NavMenu)
        assert app.tab == tab

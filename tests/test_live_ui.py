import copy

import pytest
from textual.containers import VerticalScroll
from textual.widgets import Button, Input, Static

from kanbeat.supervisor import Supervisor
from kanbeat.ui.app import KanbeatApp, NewTicket
from kanbeat.ui.board import BoardView, TicketCard
from kanbeat.ui.preferences import ViewPreferences
from kanbeat.ui.session_rows import SessionRow, SessionSection
from kanbeat.ui.sessions import SessionsView
from kanbeat.ui.startup import StartupScreen


def client(supervisor, monkeypatch):
    app = KanbeatApp(supervisor.board.project)

    async def rpc(message):
        return copy.deepcopy(await supervisor.dispatch(message))

    monkeypatch.setattr(app, "rpc", rpc)
    return app


@pytest.mark.asyncio
async def test_startup_wordmark_precedes_first_board_snapshot(tmp_path, monkeypatch):
    supervisor = Supervisor(tmp_path)
    app = KanbeatApp(tmp_path)
    screens = []

    async def rpc(message):
        screens.append(type(app.screen))
        return await supervisor.dispatch(message)

    monkeypatch.setattr(app, "rpc", rpc)
    async with app.run_test(size=(100, 35)):
        assert screens[0] is StartupScreen
        assert isinstance(app.query_one(BoardView), BoardView)
        assert not isinstance(app.screen, StartupScreen)


@pytest.mark.asyncio
async def test_runtime_refresh_preserves_card_focus_scroll_and_modal(
    tmp_path, monkeypatch
):
    supervisor = Supervisor(tmp_path)
    tickets = [supervisor.board.create(f"Task {number}") for number in range(12)]
    app = client(supervisor, monkeypatch)
    async with app.run_test(size=(150, 45)) as pilot:
        page = app.query_one(BoardView)
        card = app.query_one("#card-" + tickets[0].id, TicketCard)
        button = card.query_one(".card-title", Button)
        lane = app.query_one("#lane-backlog", VerticalScroll)
        button.focus(scroll_visible=False)
        lane.scroll_to(y=8, animate=False)
        await pilot.pause()
        scroll = lane.scroll_y
        supervisor.ticket_state(tickets[0].id)["runtime"] = {
            "developer": {"status": "running"}
        }
        await app.refresh_snapshot()
        await pilot.pause()
        assert app.query_one(BoardView) is page
        assert app.query_one("#card-" + tickets[0].id) is card
        assert app.focused is button
        assert lane.scroll_y == scroll
        assert card.query_one(".card-runtime", Static).display
        form = NewTicket()
        app.push_screen(form)
        await pilot.pause()
        field = form.query(Input).first()
        field.value = "Do not lose this text"
        supervisor.ticket_state(tickets[0].id)["runtime"]["developer"]["status"] = (
            "completed"
        )
        await app.refresh_snapshot()
        assert app.screen is form
        assert field.value == "Do not lose this text"


@pytest.mark.asyncio
async def test_board_reconciles_moves_reorders_and_removals(tmp_path, monkeypatch):
    supervisor = Supervisor(tmp_path)
    first = supervisor.board.create("First")
    second = supervisor.board.create("Second")
    app = client(supervisor, monkeypatch)
    async with app.run_test(size=(150, 50)) as pilot:
        page = app.query_one(BoardView)
        stable = app.query_one("#card-" + second.id)
        focus = app.query_one("#view-" + first.id, Button)
        focus.focus()
        await pilot.pause()
        supervisor.board.move(first.id, "todo")
        await app.refresh_snapshot()
        await pilot.pause()
        assert app.query_one(BoardView) is page
        assert app.query_one("#card-" + second.id) is stable
        moved = app.query_one("#card-" + first.id)
        assert moved.parent.id == "lane-todo"
        assert app.focused is app.query_one("#view-" + first.id)
        third = supervisor.board.create("Third")
        supervisor.board.reorder(third.id, -1)
        await app.refresh_snapshot()
        assert [
            card.ticket["id"]
            for card in app.query_one("#lane-backlog").query(TicketCard)
        ] == [third.id, second.id]
        supervisor.board.move(first.id, "archive")
        await app.refresh_snapshot()
        assert not list(app.query("#card-" + first.id))
        assert app.query_one("#card-" + second.id) is stable


@pytest.mark.asyncio
async def test_groups_contain_sessions_and_keep_collapse_across_refresh_and_restart(
    tmp_path, monkeypatch
):
    supervisor = Supervisor(tmp_path)
    group = (await supervisor.dispatch({"op": "group_create", "name": "Tools [lab]"}))[
        "group"
    ]
    entry = {
        "id": "demo",
        "title": "Shell",
        "harness": "codex",
        "status": "stopped",
        "owner": "custom",
        "group": group,
    }
    supervisor.terminals.entries["demo"] = entry
    app = client(supervisor, monkeypatch)
    async with app.run_test(size=(150, 60)) as pilot:
        await pilot.click("#nav-sessions")
        page = app.query_one(SessionsView)
        section = app.query_one("#section-group-" + group, SessionSection)
        row = app.query_one("#session-row-demo", SessionRow)
        assert row in section.query(SessionRow)
        assert not section.collapsed
        await pilot.click(f"#section-group-{group} > CollapsibleTitle")
        await pilot.pause()
        assert section.collapsed
        assert ViewPreferences(tmp_path).is_collapsed("group-" + group)
        entry["status"] = "running"
        await app.refresh_snapshot()
        await pilot.pause()
        assert app.query_one(SessionsView) is page
        assert app.query_one("#session-row-demo") is row
        assert section.collapsed
        await pilot.click("#nav-board")
        await pilot.click("#nav-sessions")
        assert app.query_one("#section-group-" + group, SessionSection).collapsed
    restarted = client(supervisor, monkeypatch)
    async with restarted.run_test(size=(150, 60)) as pilot:
        await pilot.click("#nav-sessions")
        assert restarted.query_one("#section-group-" + group, SessionSection).collapsed


@pytest.mark.asyncio
async def test_task_expansion_and_session_move_survive_updates(tmp_path, monkeypatch):
    supervisor = Supervisor(tmp_path)
    supervisor.board.create("Task")
    group = (await supervisor.dispatch({"op": "group_create", "name": "Tools"}))[
        "group"
    ]
    entry = {
        "id": "demo",
        "title": "Shell",
        "harness": "codex",
        "status": "stopped",
        "owner": "custom",
        "group": group,
    }
    supervisor.terminals.entries["demo"] = entry
    app = client(supervisor, monkeypatch)
    async with app.run_test(size=(150, 65)) as pilot:
        await pilot.click("#nav-sessions")
        tasks = app.query_one("#section-tasks", SessionSection)
        tasks.collapsed = False
        await pilot.pause()
        entry["group"] = ""
        await app.refresh_snapshot()
        await pilot.pause()
        assert app.query_one("#section-tasks") is tasks
        assert not tasks.collapsed
        row = app.query_one("#session-row-demo")
        assert row in app.query_one("#section-group-ungrouped").query(SessionRow)
        assert not list(app.query_one("#section-group-" + group).query(SessionRow))
        assert not ViewPreferences(tmp_path).is_collapsed("tasks", True)


def test_malformed_view_preferences_do_not_break_startup(tmp_path):
    path = tmp_path / ".kanbeat" / ".state" / "ui.json"
    path.parent.mkdir(parents=True)
    path.write_text("[broken")
    preferences = ViewPreferences(tmp_path)
    assert preferences.is_collapsed("tasks", True)
    preferences.remember("tasks", False)
    assert not ViewPreferences(tmp_path).is_collapsed("tasks", True)


@pytest.mark.asyncio
async def test_background_settings_updates_preserve_unsaved_fields(
    tmp_path, monkeypatch
):
    from textual.widgets import TextArea
    from kanbeat.ui.app import Settings

    supervisor = Supervisor(tmp_path)
    app = client(supervisor, monkeypatch)
    async with app.run_test(size=(140, 60)) as pilot:
        await pilot.click("#nav-settings")
        page = app.query_one(Settings)
        field = page.query_one("#cfg-developer_prompt", TextArea)
        field.load_text("Unsaved developer instructions")
        supervisor.runtime.save_config({"global_cp": "Changed elsewhere"})
        await app.refresh_snapshot()
        await app.refresh_snapshot(force=True)
        assert app.query_one(Settings) is page
        assert field.text == "Unsaved developer instructions"
        assert page.query_one("#cfg-global_cp", TextArea).text == "Changed elsewhere"


@pytest.mark.asyncio
async def test_tasks_follow_snapshot_order_after_inserting_and_moving_tickets(
    tmp_path, monkeypatch
):
    from kanbeat.ui.session_rows import TaskSessions

    supervisor = Supervisor(tmp_path)
    first = supervisor.board.create("First", column="todo")
    app = client(supervisor, monkeypatch)
    async with app.run_test(size=(140, 60)) as pilot:
        await pilot.click("#nav-sessions")
        original = app.query_one("#task-sessions-" + first.id)
        second = supervisor.board.create("New backlog")
        await app.refresh_snapshot()
        assert [row.ticket["id"] for row in app.query(TaskSessions)] == [
            second.id,
            first.id,
        ]
        assert app.query_one("#task-sessions-" + first.id) is original


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [None, "", "missing-harness", {}])
async def test_settings_refresh_handles_invalid_native_selector_values(
    tmp_path, monkeypatch, invalid
):
    from textual.widgets import Select
    from kanbeat.ui.app import Settings

    supervisor = Supervisor(tmp_path)
    app = client(supervisor, monkeypatch)
    async with app.run_test(size=(140, 60)) as pilot:
        await pilot.click("#nav-settings")
        page = app.query_one(Settings)
        supervisor.runtime.config["default_harness"] = invalid
        supervisor.runtime.config["transition_hook"] = {
            "harness": invalid,
            "prompt": "",
        }
        await app.refresh_snapshot()
        assert page.query_one("#cfg-harness", Select).value == "codex"
        assert page.query_one("#cfg-hook-harness", Select).value == ""
        supervisor.runtime.config["default_harness"] = "claude"
        await app.refresh_snapshot()
        assert page.query_one("#cfg-harness", Select).value == "claude"


@pytest.mark.asyncio
async def test_reviewer_rows_reorder_without_recreating_existing_slots(
    tmp_path, monkeypatch
):
    from kanbeat.ui.session_rows import TaskSessions

    supervisor = Supervisor(tmp_path)
    ticket = supervisor.board.create("Task")
    state = supervisor.ticket_state(ticket.id)
    state["reviewers"] = [{"id": "reviewer-2"}, {"id": "reviewer-1"}]
    app = client(supervisor, monkeypatch)
    async with app.run_test(size=(140, 60)) as pilot:
        await pilot.click("#nav-sessions")
        task = app.query_one(TaskSessions)
        original = task.query_one(f"#task-slot-{ticket.id}-reviewer-1")
        assert original.name == "reviewer-1"
        state["reviewers"].reverse()
        await app.refresh_snapshot()
        assert [row.name for row in task.query(".slot-row")] == [
            "developer",
            "reviewer-1",
            "reviewer-2",
        ]
        assert task.query_one(f"#task-slot-{ticket.id}-reviewer-1") is original


def test_concurrent_clients_preserve_other_group_choices(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    clients = [ViewPreferences(tmp_path), ViewPreferences(tmp_path)]

    def save(client_index):
        for index in range(25):
            clients[client_index].remember(f"{client_index}-{index}", bool(index % 2))

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(save, range(2)))
    restored = ViewPreferences(tmp_path)
    assert len(restored.collapsed) == 50
    assert restored.collapsed == {
        f"{client}-{index}": bool(index % 2)
        for client in range(2)
        for index in range(25)
    }

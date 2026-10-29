import asyncio
import copy

import pytest
from textual.containers import VerticalScroll
from textual.widgets import Button, Input, Static

from kanbeat.supervisor import Supervisor
from kanbeat.ui.app import Assignment, KanbeatApp, NewTicket, TicketView
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
async def test_startup_recovers_after_initial_snapshot_failure(tmp_path, monkeypatch):
    supervisor = Supervisor(tmp_path)
    app = KanbeatApp(tmp_path)
    calls = 0

    async def rpc(message):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("Snapshot temporarily unavailable")
        return await supervisor.dispatch(message)

    monkeypatch.setattr(app, "rpc", rpc)
    async with app.run_test(size=(100, 35)) as pilot:
        assert not isinstance(app.screen, StartupScreen)
        await pilot.pause(1.7)
        assert calls >= 2
        assert isinstance(app.query_one(BoardView), BoardView)


@pytest.mark.asyncio
async def test_startup_renders_board_through_supervisor_socket(tmp_path, monkeypatch):
    monkeypatch.setattr("kanbeat.ipc.socket_root", lambda: tmp_path.parent / "s")
    supervisor = Supervisor(tmp_path)
    supervisor.acquire()
    server = await asyncio.start_unix_server(
        supervisor.handle, path=str(supervisor.socket)
    )
    try:
        app = KanbeatApp(tmp_path)
        async with app.run_test(size=(100, 35)):
            assert not isinstance(app.screen, StartupScreen)
            assert isinstance(app.query_one(BoardView), BoardView)
            assert app.snapshot["execution"] == "stopped"
    finally:
        server.close()
        await server.wait_closed()
        supervisor.socket.unlink(missing_ok=True)
        supervisor.lock_stream.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(80, 24), (120, 40)])
async def test_ticket_slot_controls_have_separate_labels_and_status(
    tmp_path, monkeypatch, size
):
    supervisor = Supervisor(tmp_path)
    ticket = supervisor.board.create("Check the layout")
    state = supervisor.ticket_state(ticket.id)
    state["reviewers"] = [{"id": "reviewer-1"}]
    state["runtime"] = {"developer": {"status": "running"}}
    app = client(supervisor, monkeypatch)
    async with app.run_test(size=size) as pilot:
        view = TicketView(app.find_ticket(ticket.id))
        app.push_screen(view)
        await pilot.pause()
        for row in view.query(".slot-row"):
            label = row.query_one(".slot-name", Static)
            controls = list(row.query(Button))
            status = row.query_one(".runtime", Static)
            assert label.region.width >= 14
            assert label.region.right < controls[0].region.x
            assert all(
                before.region.right < after.region.x
                for before, after in zip(controls, controls[1:])
            )
            assert controls[-1].region.right < status.region.x
            assert label.region.y <= status.region.y < label.region.bottom
        assert str(view.query_one("#developer-runtime", Static).render()) == "running"
        assert (
            str(view.query_one("#runtime-reviewer-1", Static).render()) == "not started"
        )


@pytest.mark.asyncio
async def test_assignment_actions_remain_reachable_in_compact_terminal(
    tmp_path, monkeypatch
):
    supervisor = Supervisor(tmp_path)
    ticket = supervisor.board.create("Compact form")
    app = client(supervisor, monkeypatch)
    async with app.run_test(size=(80, 24)) as pilot:
        form = Assignment(app.find_ticket(ticket.id), "developer")
        app.push_screen(form)
        await pilot.pause()
        form.query_one(".sm", VerticalScroll).scroll_end(animate=False)
        await pilot.pause()
        assert form.query_one("#save", Button).region.bottom <= 24


@pytest.mark.asyncio
async def test_invalid_harness_setting_has_visible_warning(tmp_path, monkeypatch):
    from kanbeat.ui.settings import Settings

    supervisor = Supervisor(tmp_path)
    app = client(supervisor, monkeypatch)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.click("#nav-settings")
        page = app.query_one(Settings)
        supervisor.runtime.config["default_harness"] = "removed-harness"
        supervisor.runtime.config["transition_hook"]["harness"] = "missing-hook"
        await app.refresh_snapshot()
        first = page.query_one("#cfg-harness-warning", Static)
        second = page.query_one("#cfg-hook-harness-warning", Static)
        assert first.display and "removed-harness" in str(first.render())
        assert second.display and "missing-hook" in str(second.render())


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(80, 24), (130, 35)])
async def test_board_columns_keep_compact_width_and_order(tmp_path, monkeypatch, size):
    supervisor = Supervisor(tmp_path)
    supervisor.board.create("A task")
    app = client(supervisor, monkeypatch)
    async with app.run_test(size=size):
        lanes = list(app.query("#lanes .lane"))
        assert len(lanes) == 6
        assert [lane.region.width for lane in lanes] == [20] * 6
        assert [
            after.region.x - before.region.x for before, after in zip(lanes, lanes[1:])
        ] == [21] * 5
        actions = list(app.query("#board-page .toolbar Button"))
        assert all(
            before.region.right < after.region.x
            for before, after in zip(actions, actions[1:])
        )


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


@pytest.mark.asyncio
async def test_task_session_process_status_updates_without_rebuilding(
    tmp_path, monkeypatch
):
    from kanbeat.ui.session_rows import TaskSessions

    supervisor = Supervisor(tmp_path)
    ticket = supervisor.board.create("Lifecycle")
    state = supervisor.ticket_state(ticket.id)
    state["reviewers"] = [{"id": "reviewer-1"}]
    app = client(supervisor, monkeypatch)
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.click("#nav-sessions")
        tasks = app.query_one("#section-tasks", SessionSection)
        tasks.collapsed = False
        await pilot.pause()
        task_row = app.query_one("#task-sessions-" + ticket.id, TaskSessions)
        developer = task_row.query_one(f"#task-status-{ticket.id}-developer", Static)
        reviewer = task_row.query_one(f"#task-status-{ticket.id}-reviewer-1", Static)
        assert str(developer.render()) == "not started"
        state["runtime"] = {
            "developer": {"status": "running"},
            "reviewer-1": {"status": "starting"},
        }
        await app.refresh_snapshot()
        assert app.query_one("#task-sessions-" + ticket.id) is task_row
        assert task_row.query_one(f"#task-status-{ticket.id}-developer") is developer
        assert str(developer.render()) == "Worker running"
        assert str(reviewer.render()) == "Worker starting"

        state["runtime"]["developer"] = {
            "status": "failed",
            "error": "native call failed",
            "exit_code": 17,
        }
        supervisor.terminals.entries["task-pty"] = {
            "id": "task-pty",
            "owner": "board",
            "ticket": ticket.id,
            "slot": "reviewer-1",
            "harness": "codex",
            "title": "Reviewer",
            "status": "running",
        }
        await app.refresh_snapshot()
        assert "failed (exit 17)" in str(developer.render())
        assert "native call failed" in str(developer.render())
        assert "PTY running" in str(reviewer.render())
        assert app.query_one("#section-tasks") is tasks
        assert not tasks.collapsed

        supervisor.terminals.entries["task-pty"].update(status="exited", exit_code=9)
        await app.refresh_snapshot()
        assert "PTY exited (exit 9)" in str(reviewer.render())


def test_independent_session_label_includes_exit_and_error():
    from kanbeat.ui.session_rows import SessionRow

    entry = {
        "id": "example",
        "title": "Tool",
        "harness": "pi",
        "status": "exited",
        "exit_code": 5,
        "error": "native\nfailure\r\nfrom\tprovider\x1b",
    }
    assert "exited (exit 5)" in SessionRow.label(entry)
    assert "native failure from provider" in SessionRow.label(entry)
    assert "\n" not in SessionRow.label(entry)
    assert "\r" not in SessionRow.label(entry)
    assert "\x1b" not in SessionRow.label(entry)


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

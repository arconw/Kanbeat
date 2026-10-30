import sys

import pytest
from textual.widgets import Button, Input, Select

from kanbeat.supervisor import Supervisor
from kanbeat.ui.app import Confirm, KanbeatApp
from kanbeat.ui.sessions import SessionActions
from kanbeat.ui.terminal import TerminalView


@pytest.fixture
def supervisor(tmp_path, monkeypatch):
    script = "import sys; print('READY',flush=True); [print('ECHO:'+line.strip(),flush=True) for line in sys.stdin]"
    monkeypatch.setattr(
        "kanbeat.terminals.interactive_command",
        lambda *args: [sys.executable, "-u", "-c", script],
    )
    monkeypatch.setattr("kanbeat.session_groups.available", lambda name: True)
    monkeypatch.setattr("kanbeat.supervisor.available", lambda name: True)
    instance = Supervisor(tmp_path)
    instance.runtime.config["default_harness"] = "pi"
    return instance


@pytest.mark.asyncio
async def test_groups_sessions_and_board_ownership(supervisor):
    ticket = supervisor.board.create("Task")
    original = ticket.path.read_bytes()
    group = (await supervisor.dispatch({"op": "group_create", "name": "Research"}))[
        "group"
    ]
    await supervisor.dispatch({"op": "group_rename", "group": group, "name": "Tools"})
    terminal_id = (
        await supervisor.dispatch(
            {
                "op": "session_create",
                "group": group,
                "title": "CLI",
                "harness": "codex",
                "prompt": "Initial instructions",
            }
        )
    )["terminal"]
    session = supervisor.terminals.get(terminal_id)
    try:
        await supervisor.play()
        await supervisor.stop()
        assert session.entry["status"] == "running"
        with pytest.raises(ValueError, match="confirmation"):
            await supervisor.dispatch({"op": "terminal_stop", "terminal": terminal_id})
        with pytest.raises(ValueError, match="Move or remove"):
            await supervisor.dispatch(
                {"op": "group_delete", "group": group, "confirmed": True}
            )
        await supervisor.dispatch(
            {
                "op": "session_update",
                "terminal": terminal_id,
                "title": "Renamed",
                "group": "",
            }
        )
        await supervisor.dispatch(
            {"op": "group_delete", "group": group, "confirmed": True}
        )
        assert session.entry["title"] == "Renamed"
        assert session.entry["group"] == ""
        assert ticket.path.read_bytes() == original
    finally:
        await supervisor.terminals.stop()


@pytest.mark.asyncio
async def test_harness_config_session_is_reused_and_restarted(supervisor):
    first = (
        await supervisor.dispatch({"op": "session_config_open", "harness": "claude"})
    )["terminal"]
    entry = supervisor.terminals.entries[first]
    assert entry["owner"] == "custom"
    assert entry["role"] == "config"
    assert entry["harness"] == "claude"
    assert entry["title"] == "Configure claude"
    try:
        again = await supervisor.dispatch(
            {"op": "session_config_open", "harness": "claude"}
        )
        assert again["terminal"] == first
        assert len(supervisor.terminals.sessions) == 1
        await supervisor.terminals.stop()
        assert entry["status"] == "stopped"
        reopened = await supervisor.dispatch(
            {"op": "session_config_open", "harness": "claude"}
        )
        assert reopened["terminal"] == first
        assert supervisor.terminals.get(first).entry["status"] == "running"
        default = await supervisor.dispatch(
            {"op": "session_config_open", "harness": ""}
        )
        default_entry = supervisor.terminals.entries[default["terminal"]]
        assert default_entry["harness"] == "pi"
        entry["status"] = "stopping"
        supervisor.save()
        with pytest.raises(ValueError, match="still stopping"):
            await supervisor.dispatch(
                {"op": "session_config_open", "harness": "claude"}
            )
    finally:
        await supervisor.terminals.stop()


@pytest.mark.asyncio
async def test_harness_config_session_recovers_detached_active_record(supervisor):
    terminal_id = (
        await supervisor.dispatch({"op": "session_config_open", "harness": "pi"})
    )["terminal"]
    entry = supervisor.terminals.entries[terminal_id]
    try:
        await supervisor.terminals.stop()
        supervisor.terminals.sessions.pop(terminal_id)
        entry["status"] = "running"
        supervisor.save()
        reopened = await supervisor.dispatch(
            {"op": "session_config_open", "harness": "pi"}
        )
        assert reopened["terminal"] == terminal_id
        assert supervisor.terminals.get(terminal_id).entry["status"] == "running"
    finally:
        await supervisor.terminals.stop()


@pytest.mark.asyncio
async def test_harness_config_session_rejects_unavailable_harness(
    supervisor, monkeypatch
):
    monkeypatch.setattr("kanbeat.session_groups.available", lambda name: name != "pi")
    with pytest.raises(ValueError, match="unavailable: pi"):
        await supervisor.dispatch({"op": "session_config_open", "harness": "pi"})
    assert not supervisor.terminals.entries


@pytest.mark.asyncio
async def test_restart_retains_identity_and_persisted_settings(supervisor):
    group = (await supervisor.dispatch({"op": "group_create", "name": "Tools"}))[
        "group"
    ]
    terminal_id = (
        await supervisor.dispatch(
            {
                "op": "session_create",
                "group": group,
                "title": "CLI",
                "harness": "claude",
                "native_session": "native-123",
                "prompt": "Initial",
            }
        )
    )["terminal"]
    await supervisor.terminals.stop()
    restored = Supervisor(supervisor.board.project)
    await restored.recover_orphans()
    assert restored.session_groups.groups[group]["name"] == "Tools"
    assert restored.terminals.entries[terminal_id]["native_session"] == "native-123"
    assert restored.terminals.entries[terminal_id]["prompt"] == "Initial"
    assert not restored.snapshot()["terminals"][terminal_id]["attached"]
    result = await restored.dispatch({"op": "session_restart", "terminal": terminal_id})
    try:
        assert result == {"terminal": terminal_id}
        assert restored.terminals.get(terminal_id).entry["status"] == "running"
        assert restored.snapshot()["terminals"][terminal_id]["attached"]
    finally:
        await restored.terminals.stop()


@pytest.mark.asyncio
async def test_remove_requires_stopped_session_and_confirmation(supervisor):
    terminal_id = (await supervisor.dispatch({"op": "session_create", "title": "CLI"}))[
        "terminal"
    ]
    try:
        with pytest.raises(ValueError, match="Stop the session"):
            await supervisor.dispatch(
                {"op": "session_delete", "terminal": terminal_id, "confirmed": True}
            )
        await supervisor.terminals.stop()
        with pytest.raises(ValueError, match="confirmation"):
            await supervisor.dispatch({"op": "session_delete", "terminal": terminal_id})
        await supervisor.dispatch(
            {"op": "session_delete", "terminal": terminal_id, "confirmed": True}
        )
        assert terminal_id not in supervisor.terminals.entries
        assert terminal_id not in supervisor.terminals.sessions
        assert (supervisor.state_dir / "logs" / f"terminal-{terminal_id}.log").exists()
    finally:
        await supervisor.terminals.stop()


@pytest.mark.asyncio
async def test_group_validation_is_non_mutating(supervisor):
    group = (await supervisor.dispatch({"op": "group_create", "name": "Tools"}))[
        "group"
    ]
    for name in ("", " " * 5, "x" * 65, "Bad\nName", "tools"):
        with pytest.raises(ValueError):
            await supervisor.dispatch({"op": "group_create", "name": name})
    with pytest.raises(ValueError, match="Unknown"):
        await supervisor.dispatch(
            {"op": "session_create", "title": "CLI", "group": "missing"}
        )
    assert supervisor.session_groups.groups == {group: {"name": "Tools"}}
    assert not supervisor.terminals.entries


@pytest.mark.asyncio
async def test_missing_custom_harness_does_not_stop_board(supervisor, monkeypatch):
    supervisor.runtime.data["execution"] = "running"
    monkeypatch.setattr("kanbeat.session_groups.available", lambda name: False)
    with pytest.raises(ValueError, match="unavailable"):
        await supervisor.dispatch({"op": "session_create", "title": "CLI"})
    assert supervisor.runtime.data["execution"] == "running"
    assert not supervisor.terminals.entries


@pytest.mark.asyncio
async def test_session_controls_cannot_remove_board_terminal(supervisor):
    ticket = supervisor.board.create("Task")
    terminal_id = (
        await supervisor.dispatch({"op": "terminal_open", "ticket": ticket.id})
    )["terminal"]
    try:
        with pytest.raises(ValueError, match="Unknown independent"):
            await supervisor.dispatch(
                {"op": "session_delete", "terminal": terminal_id, "confirmed": True}
            )
    finally:
        await supervisor.terminals.stop()


@pytest.mark.asyncio
async def test_tui_group_creation_and_independent_stop_confirmation(
    supervisor, monkeypatch
):
    app = KanbeatApp(supervisor.board.project)
    monkeypatch.setattr(app, "rpc", supervisor.dispatch)
    try:
        async with app.run_test(size=(120, 65)) as pilot:
            await pilot.click("#nav-sessions")
            await pilot.click("#new-group")
            app.screen.query_one("#group-name", Input).value = "Tools [lab]"
            await pilot.click("#group-save")
            await pilot.pause()
            group_id = next(iter(supervisor.session_groups.groups))
            await pilot.click("#new-session")
            app.screen.query_one("#session-title", Input).value = "Independent CLI"
            app.screen.query_one("#session-group", Select).value = group_id
            app.screen.query_one("#session-start", Button).press()
            await pilot.pause()
            assert isinstance(app.screen, TerminalView)
            terminal_id = next(iter(supervisor.terminals.entries))
            await pilot.pause(1.6)
            await pilot.click("#terminal-detach")
            await pilot.pause()
            await pilot.click("#session-actions-" + terminal_id)
            await pilot.click("#session-stop")
            await pilot.pause()
            assert isinstance(app.screen, Confirm)
            await pilot.click("#cancel")
            await pilot.pause()
            assert isinstance(app.screen, SessionActions)
            assert supervisor.terminals.entries[terminal_id]["status"] == "running"
            await pilot.click("#session-stop")
            await pilot.click("#yes")
            await pilot.pause()
            assert supervisor.terminals.entries[terminal_id]["status"] == "stopped"
    finally:
        await supervisor.terminals.stop()


@pytest.mark.asyncio
async def test_recovery_blocked_session_can_restart_after_manual_resolution(
    supervisor, monkeypatch
):
    terminal_id = (await supervisor.dispatch({"op": "session_create", "title": "CLI"}))[
        "terminal"
    ]
    await supervisor.terminals.stop()
    entry = supervisor.terminals.entries[terminal_id]
    entry.update(status="recovery_blocked", pid=12345, process_identity="saved")
    monkeypatch.setattr("kanbeat.session_groups.process_identity", lambda pid: "saved")
    with pytest.raises(ValueError, match="resolve orphan"):
        await supervisor.dispatch({"op": "session_restart", "terminal": terminal_id})
    with pytest.raises(ValueError, match="resolve orphan"):
        await supervisor.dispatch(
            {"op": "session_delete", "terminal": terminal_id, "confirmed": True}
        )
    monkeypatch.setattr("kanbeat.session_groups.process_identity", lambda pid: None)
    try:
        assert await supervisor.dispatch(
            {"op": "session_restart", "terminal": terminal_id}
        ) == {"terminal": terminal_id}
        assert entry["status"] == "running"
    finally:
        await supervisor.terminals.stop()


@pytest.mark.asyncio
async def test_group_removal_keeps_sessions_page_and_controls(supervisor, monkeypatch):
    from kanbeat.ui.sessions import GroupForm, SessionsView

    group_id = (
        await supervisor.dispatch({"op": "group_create", "name": "Disposable"})
    )["group"]
    app = KanbeatApp(supervisor.board.project)
    monkeypatch.setattr(app, "rpc", supervisor.dispatch)
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.click("#nav-sessions")
        await pilot.click("#remove-group-" + group_id)
        await pilot.pause()
        assert isinstance(app.screen, Confirm)
        await pilot.click("#yes")
        await pilot.pause()
        assert group_id not in supervisor.session_groups.groups
        assert app.query_one(SessionsView).is_mounted
        await pilot.click("#new-group")
        await pilot.pause()
        assert isinstance(app.screen, GroupForm)

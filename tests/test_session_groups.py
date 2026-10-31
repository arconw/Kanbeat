import asyncio
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.cells import cell_len
from textual.geometry import Region
from textual.widgets import Button, Input, Select

from kanbeat.supervisor import Supervisor
from kanbeat.ui.app import Confirm, KanbeatApp
from kanbeat.ui.sessions import NewSession, SessionActions
from kanbeat.ui.terminal import TerminalPane, TerminalView


SCRIPT = "import sys; print('READY',flush=True); [print('ECHO:'+line.strip(),flush=True) for line in sys.stdin]"


class FakeCodexHost:
    def __init__(self):
        self.threads = []
        self.launches = []
        self.deleted = []

    async def delete_thread(self, thread_id):
        self.deleted.append(thread_id)

    async def start_thread(self, cwd, model=None, model_provider=None):
        thread_id = f"thread-{len(self.threads)}"
        self.threads.append(thread_id)
        return SimpleNamespace(thread_id=thread_id)

    async def set_thread_name(self, thread_id, name):
        pass

    async def observe_thread(self, thread_id):
        assert thread_id in self.threads

    def tui_argv(self, *, thread_id=None, prompt=None):
        self.launches.append((thread_id, prompt))
        return [sys.executable, "-u", "-c", SCRIPT]

    def child_env(self):
        return dict(os.environ)


class FakeOpenCodeHost:
    def __init__(self):
        self.sessions = []
        self.deleted = []

    async def delete_session(self, session_id):
        self.deleted.append(session_id)

    async def create_session(self, directory=None, model=None):
        session_id = f"ses_{len(self.sessions)}"
        self.sessions.append(session_id)
        return SimpleNamespace(session_id=session_id)

    async def read_session(self, session_id):
        assert session_id in self.sessions

    def viewer_argv(self, session_id):
        return [sys.executable, "-u", "-c", SCRIPT]

    def child_env(self):
        return dict(os.environ)


async def log_has(log, text, count=1):
    for _ in range(300):
        if log.exists() and log.read_text().count(text) >= count:
            return log.read_text()
        await asyncio.sleep(0.02)
    pytest.fail(f"{log.name} has no {text!r}")


async def claude_log_has(supervisor, text, count=1):
    return await log_has(supervisor.claude_log, text, count)


@pytest.fixture
def supervisor(tmp_path, tmp_path_factory, monkeypatch):
    monkeypatch.setattr(
        "kanbeat.terminals.interactive_command",
        lambda *args: [sys.executable, "-u", "-c", SCRIPT],
    )
    bin_dir = tmp_path_factory.mktemp("bin")
    claude_log = bin_dir / "claude.log"
    pi_log = bin_dir / "pi.log"
    for name, log in (("claude", claude_log), ("pi", pi_log)):
        fake = bin_dir / name
        fake.write_text(
            f"#!{sys.executable}\nimport sys\n"
            f"open({str(log)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
            f"exec({SCRIPT!r})\n"
        )
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv(
        "PI_CODING_AGENT_SESSION_DIR", str(tmp_path_factory.mktemp("pi-sessions"))
    )
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path_factory.mktemp("claude")))
    monkeypatch.setattr("kanbeat.session_groups.available", lambda name: True)
    monkeypatch.setattr("kanbeat.supervisor.available", lambda name: True)
    instance = Supervisor(tmp_path)
    instance.runtime.config["default_harness"] = "pi"
    instance.fake_codex = FakeCodexHost()
    instance.fake_opencode = FakeOpenCodeHost()

    async def codex_host():
        return instance.fake_codex

    async def opencode_host():
        return instance.fake_opencode

    instance.session_groups.codex_host = codex_host
    instance.session_groups.opencode_host = opencode_host
    instance.claude_log = claude_log
    instance.pi_log = pi_log
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
    first = (await supervisor.dispatch({"op": "session_config_open", "harness": "pi"}))[
        "terminal"
    ]
    entry = supervisor.terminals.entries[first]
    assert entry["owner"] == "custom"
    assert entry["role"] == "config"
    assert entry["harness"] == "pi"
    assert entry["title"] == "Configure pi"
    try:
        again = await supervisor.dispatch(
            {"op": "session_config_open", "harness": "pi"}
        )
        assert again["terminal"] == first
        assert len(supervisor.terminals.sessions) == 1
        await supervisor.terminals.stop()
        assert entry["status"] == "stopped"
        reopened = await supervisor.dispatch(
            {"op": "session_config_open", "harness": "pi"}
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
            await supervisor.dispatch({"op": "session_config_open", "harness": "pi"})
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
async def test_tui_group_creation_and_independent_session_stop(supervisor, monkeypatch):
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
            terminal_id = next(iter(supervisor.terminals.entries))
            assert not isinstance(app.screen, TerminalView)
            assert app.query_one(TerminalPane).terminal_id == terminal_id
            await pilot.pause(1.6)
            await pilot.click("#terminal-detach")
            await pilot.pause()
            await pilot.click("#session-actions-" + terminal_id)
            await pilot.click("#session-stop")
            await pilot.pause(0.5)
            assert not isinstance(app.screen, (Confirm, SessionActions))
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
        await pilot.click("#group-actions-" + group_id)
        await pilot.pause()
        await pilot.click("#group-remove")
        await pilot.pause()
        assert isinstance(app.screen, Confirm)
        await pilot.click("#yes")
        await pilot.pause()
        assert group_id not in supervisor.session_groups.groups
        assert app.query_one(SessionsView).is_mounted
        await pilot.click("#new-group")
        await pilot.pause()
        assert isinstance(app.screen, GroupForm)


@pytest.mark.asyncio
async def test_sessions_page_switches_embedded_terminal_between_sessions(
    supervisor, monkeypatch
):
    first = (
        await supervisor.dispatch(
            {"op": "session_create", "title": "First", "harness": "codex"}
        )
    )["terminal"]
    second = (
        await supervisor.dispatch(
            {"op": "session_create", "title": "Second", "harness": "codex"}
        )
    )["terminal"]
    app = KanbeatApp(supervisor.board.project)
    monkeypatch.setattr(app, "rpc", supervisor.dispatch)
    try:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.click("#nav-sessions")
            await pilot.pause(0.3)
            pane = app.query_one(TerminalPane)
            assert pane.terminal_id is None
            assert pane.size.width >= 0.7 * 140
            await pilot.click("#open-session-" + first)
            await pilot.pause(0.5)
            assert pane.terminal_id == first
            assert not isinstance(app.screen, TerminalView)
            await pilot.click("#session-row-" + second, offset=(5, 1))
            await pilot.pause(0.5)
            assert pane.terminal_id == second
            assert pane.revision >= 0
            assert supervisor.terminals.get(first).entry["status"] == "running"
            exits = []
            monkeypatch.setattr(app, "exit", lambda *args, **kwargs: exits.append(1))
            await pilot.click("#quit")
            await pilot.pause()
            assert exits == [1]
            await pilot.click("#nav-board")
            await pilot.click("#nav-sessions")
            await pilot.pause(0.5)
            assert app.query_one(TerminalPane).terminal_id == second
            await pilot.click("#terminal-detach")
            await pilot.pause(0.2)
            assert app.query_one(TerminalPane).terminal_id is None
            assert app.embedded_terminal is None
    finally:
        await supervisor.terminals.stop()


@pytest.mark.asyncio
async def test_sessions_is_the_first_and_default_tab(supervisor, monkeypatch):
    app = KanbeatApp(supervisor.board.project)
    monkeypatch.setattr(app, "rpc", supervisor.dispatch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.3)
        buttons = [button.id for button in app.query(".nav-button")]
        assert buttons == ["nav-sessions", "nav-board", "nav-archive", "nav-settings"]
        assert app.tab == "sessions"
        assert app.query_one("#sessions-page").is_mounted


@pytest.mark.asyncio
async def test_codex_session_gets_a_thread_up_front_and_restart_reuses_it(supervisor):
    terminal_id = (
        await supervisor.dispatch(
            {
                "op": "session_create",
                "title": "CDX",
                "harness": "codex",
                "prompt": "Hello",
            }
        )
    )["terminal"]
    try:
        entry = supervisor.terminals.entries[terminal_id]
        assert entry["native_session"] == "thread-0"
        assert supervisor.fake_codex.launches == [
            ("thread-0", "Kanbeat interactive instructions:\nHello")
        ]
        await supervisor.terminals.stop()
        await supervisor.dispatch({"op": "session_restart", "terminal": terminal_id})
        assert supervisor.fake_codex.threads == ["thread-0"]
        assert supervisor.fake_codex.launches[-1] == ("thread-0", None)
        assert entry["native_session"] == "thread-0"
    finally:
        await supervisor.terminals.stop()


@pytest.mark.asyncio
async def test_claude_session_is_preassigned_then_resumed_once_it_has_a_transcript(
    supervisor,
):
    terminal_id = (
        await supervisor.dispatch(
            {"op": "session_create", "title": "CC", "harness": "claude"}
        )
    )["terminal"]
    try:
        entry = supervisor.terminals.entries[terminal_id]
        session_id = entry["native_session"]
        assert entry["native_assigned"] is True
        await claude_log_has(supervisor, f"--session-id {session_id}")
        await supervisor.terminals.stop()
        await supervisor.dispatch({"op": "session_restart", "terminal": terminal_id})
        await claude_log_has(supervisor, "--session-id", count=2)
        await supervisor.terminals.stop()
        transcripts = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / "p"
        transcripts.mkdir(parents=True)
        (transcripts / f"{session_id}.jsonl").write_text("{}\n")
        await supervisor.dispatch({"op": "session_restart", "terminal": terminal_id})
        await claude_log_has(supervisor, f"--resume {session_id}")
        assert entry["native_session"] == session_id
    finally:
        await supervisor.terminals.stop()


@pytest.mark.asyncio
async def test_claude_session_with_a_user_supplied_id_is_resumed_not_recreated(
    supervisor,
):
    terminal_id = (
        await supervisor.dispatch(
            {
                "op": "session_create",
                "title": "CC",
                "harness": "claude",
                "native_session": "my-id",
            }
        )
    )["terminal"]
    try:
        await supervisor.terminals.stop()
        await supervisor.dispatch({"op": "session_restart", "terminal": terminal_id})
        log = await claude_log_has(supervisor, "--resume my-id")
        assert "--session-id" not in log
    finally:
        await supervisor.terminals.stop()


@pytest.mark.asyncio
async def test_opencode_session_uses_the_shared_host_unless_a_prompt_is_given(
    supervisor,
):
    plain = (
        await supervisor.dispatch(
            {"op": "session_create", "title": "OC", "harness": "opencode"}
        )
    )["terminal"]
    prompted = (
        await supervisor.dispatch(
            {
                "op": "session_create",
                "title": "OC2",
                "harness": "opencode",
                "prompt": "Hi",
            }
        )
    )["terminal"]
    try:
        assert supervisor.terminals.entries[plain]["native_session"] == "ses_0"
        assert supervisor.terminals.entries[prompted]["native_session"] is None
        assert supervisor.fake_opencode.sessions == ["ses_0"]
    finally:
        await supervisor.terminals.stop()


@pytest.mark.asyncio
async def test_double_pressing_start_creates_a_single_session(supervisor, monkeypatch):
    app = KanbeatApp(supervisor.board.project)

    async def slow_rpc(message):
        await asyncio.sleep(0.2)
        return await supervisor.dispatch(message)

    monkeypatch.setattr(app, "rpc", slow_rpc)
    try:
        async with app.run_test(size=(120, 50)) as pilot:
            await pilot.click("#new-session")
            await pilot.pause()
            app.screen.query_one("#session-title", Input).value = "Once"
            button = app.screen.query_one("#session-start", Button)
            button.press()
            button.press()
            await pilot.pause(1.0)
            assert not isinstance(app.screen, NewSession)
            titles = [e["title"] for e in supervisor.terminals.entries.values()]
            assert titles == ["Once"]
    finally:
        await supervisor.terminals.stop()


@pytest.mark.asyncio
async def test_native_id_can_be_attached_to_a_stopped_session_and_is_resumed(
    supervisor,
):
    terminal_id = (
        await supervisor.dispatch(
            {"op": "session_create", "title": "Old", "harness": "claude"}
        )
    )["terminal"]
    try:
        entry = supervisor.terminals.entries[terminal_id]
        update = {
            "op": "session_update",
            "terminal": terminal_id,
            "title": "Old",
            "group": "",
            "native_session": "recovered-id",
        }
        with pytest.raises(ValueError, match="Stop the session"):
            await supervisor.dispatch(update)
        await supervisor.terminals.stop()
        with pytest.raises(ValueError, match="Invalid native"):
            await supervisor.dispatch({**update, "native_session": "two words"})
        await supervisor.dispatch(update)
        assert entry["native_session"] == "recovered-id"
        assert "native_assigned" not in entry
        await supervisor.dispatch({"op": "session_restart", "terminal": terminal_id})
        await claude_log_has(supervisor, "--resume recovered-id")
        await supervisor.terminals.stop()
        await supervisor.dispatch({**update, "native_session": ""})
        assert entry["native_session"] is None
    finally:
        await supervisor.terminals.stop()


@pytest.mark.asyncio
async def test_clicking_a_stopped_session_resumes_it_in_the_pane(
    supervisor, monkeypatch
):
    terminal_id = (
        await supervisor.dispatch(
            {"op": "session_create", "title": "Sleepy", "harness": "codex"}
        )
    )["terminal"]
    await supervisor.terminals.stop()
    app = KanbeatApp(supervisor.board.project)
    monkeypatch.setattr(app, "rpc", supervisor.dispatch)
    try:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause(0.4)
            assert supervisor.terminals.entries[terminal_id]["status"] == "stopped"
            await pilot.click("#session-row-" + terminal_id, offset=(5, 1))
            await pilot.pause(0.8)
            assert supervisor.terminals.entries[terminal_id]["status"] == "running"
            assert app.query_one(TerminalPane).terminal_id == terminal_id
            assert supervisor.fake_codex.threads == ["thread-0"]
    finally:
        await supervisor.terminals.stop()


@pytest.mark.asyncio
async def test_session_menu_offers_delete_and_complete_delete_without_resume(
    supervisor, monkeypatch
):
    known = (
        await supervisor.dispatch(
            {"op": "session_create", "title": "Known", "harness": "codex"}
        )
    )["terminal"]
    legacy = (await supervisor.dispatch({"op": "session_create", "title": "Legacy"}))[
        "terminal"
    ]
    supervisor.terminals.entries[legacy]["native_session"] = None
    app = KanbeatApp(supervisor.board.project)
    monkeypatch.setattr(app, "rpc", supervisor.dispatch)
    try:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause(0.4)
            await pilot.click("#session-actions-" + known)
            await pilot.pause()
            assert not app.screen.query("#session-restart")
            assert app.screen.query("#session-stop")
            assert app.screen.query("#session-remove")
            assert app.screen.query("#session-remove-all")
            assert not app.screen.query("#cancel")
            await pilot.mouse_down(offset=(130, 35))
            await pilot.pause()
            assert not isinstance(app.screen, SessionActions)
            await pilot.click("#session-actions-" + known)
            await pilot.pause()
            await pilot.mouse_down("#session-stop")
            await pilot.pause()
            assert isinstance(app.screen, SessionActions)
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, SessionActions)
            await pilot.click("#session-actions-" + legacy)
            await pilot.pause()
            assert app.screen.query("#session-remove")
            assert not app.screen.query("#session-remove-all")
    finally:
        await supervisor.terminals.stop()


@pytest.mark.asyncio
async def test_delete_stops_a_running_session_and_removes_only_the_workspace_record(
    supervisor, monkeypatch
):
    terminal_id = (
        await supervisor.dispatch(
            {"op": "session_create", "title": "Gone", "harness": "codex"}
        )
    )["terminal"]
    app = KanbeatApp(supervisor.board.project)
    monkeypatch.setattr(app, "rpc", supervisor.dispatch)
    try:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause(0.4)
            await pilot.click("#session-actions-" + terminal_id)
            await pilot.click("#session-remove")
            await pilot.pause()
            assert isinstance(app.screen, Confirm)
            await pilot.click("#yes")
            await pilot.pause(0.8)
            assert terminal_id not in supervisor.terminals.entries
            assert supervisor.fake_codex.deleted == []
    finally:
        await supervisor.terminals.stop()


@pytest.mark.asyncio
async def test_complete_delete_removes_the_native_session_per_harness(supervisor):
    codex = (
        await supervisor.dispatch(
            {"op": "session_create", "title": "C", "harness": "codex"}
        )
    )["terminal"]
    opencode = (
        await supervisor.dispatch(
            {"op": "session_create", "title": "O", "harness": "opencode"}
        )
    )["terminal"]
    claude = (
        await supervisor.dispatch(
            {"op": "session_create", "title": "L", "harness": "claude"}
        )
    )["terminal"]
    claude_id = supervisor.terminals.entries[claude]["native_session"]
    transcript_dir = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / "p"
    (transcript_dir / claude_id).mkdir(parents=True)
    (transcript_dir.parent / "p" / f"{claude_id}.jsonl").write_text("{}\n")
    other = transcript_dir / "keep.jsonl"
    other.write_text("{}\n")
    await supervisor.terminals.stop()
    try:
        for terminal_id in (codex, opencode, claude):
            await supervisor.dispatch(
                {
                    "op": "session_delete",
                    "terminal": terminal_id,
                    "confirmed": True,
                    "native": True,
                }
            )
        assert supervisor.fake_codex.deleted == ["thread-0"]
        assert supervisor.fake_opencode.deleted == ["ses_0"]
        assert not (transcript_dir / f"{claude_id}.jsonl").exists()
        assert not (transcript_dir / claude_id).exists()
        assert other.exists()
        assert not supervisor.terminals.entries
    finally:
        await supervisor.terminals.stop()


@pytest.mark.asyncio
async def test_complete_delete_keeps_the_record_when_the_harness_refuses(supervisor):
    terminal_id = (
        await supervisor.dispatch(
            {"op": "session_create", "title": "Pi", "harness": "pi"}
        )
    )["terminal"]
    await supervisor.terminals.stop()
    supervisor.terminals.entries[terminal_id]["native_session"] = None
    with pytest.raises(ValueError, match="nothing to delete"):
        await supervisor.dispatch(
            {
                "op": "session_delete",
                "terminal": terminal_id,
                "confirmed": True,
                "native": True,
            }
        )
    assert terminal_id in supervisor.terminals.entries


@pytest.mark.asyncio
async def test_pi_session_is_preassigned_and_always_resumed_by_session_id(supervisor):
    terminal_id = (
        await supervisor.dispatch(
            {"op": "session_create", "title": "P", "harness": "pi", "prompt": "Hi"}
        )
    )["terminal"]
    try:
        entry = supervisor.terminals.entries[terminal_id]
        session_id = entry["native_session"]
        assert entry["native_assigned"] is True
        first = await log_has(supervisor.pi_log, f"--session-id {session_id}")
        assert "Kanbeat interactive instructions" in first
        await supervisor.terminals.stop()
        sessions = Path(os.environ["PI_CODING_AGENT_SESSION_DIR"]) / "--project--"
        sessions.mkdir(parents=True)
        (sessions / f"2026-10-30T00-00-00-000Z_{session_id}.jsonl").write_text("{}\n")
        await supervisor.dispatch({"op": "session_restart", "terminal": terminal_id})
        log = await log_has(supervisor.pi_log, f"--session-id {session_id}", count=2)
        assert log.count("Kanbeat interactive instructions") == 1
        assert entry["native_session"] == session_id
    finally:
        await supervisor.terminals.stop()


@pytest.mark.asyncio
async def test_pi_session_with_a_user_supplied_id_uses_the_legacy_session_flag(
    supervisor,
):
    terminal_id = (
        await supervisor.dispatch(
            {
                "op": "session_create",
                "title": "P",
                "harness": "pi",
                "native_session": "abc123",
            }
        )
    )["terminal"]
    try:
        assert supervisor.terminals.entries[terminal_id]["native_session"] == "abc123"
        assert "--session-id" not in (
            supervisor.pi_log.read_text() if supervisor.pi_log.exists() else ""
        )
    finally:
        await supervisor.terminals.stop()


@pytest.mark.asyncio
async def test_complete_delete_removes_the_pi_session_file(supervisor):
    terminal_id = (
        await supervisor.dispatch(
            {"op": "session_create", "title": "P", "harness": "pi"}
        )
    )["terminal"]
    session_id = supervisor.terminals.entries[terminal_id]["native_session"]
    folder = Path(os.environ["PI_CODING_AGENT_SESSION_DIR"]) / "--project--"
    folder.mkdir(parents=True)
    target = folder / f"2026-10-30T00-00-00-000Z_{session_id}.jsonl"
    other = folder / "2026-10-30T00-00-00-000Z_other.jsonl"
    target.write_text("{}\n")
    other.write_text("{}\n")
    await supervisor.terminals.stop()
    await supervisor.dispatch(
        {
            "op": "session_delete",
            "terminal": terminal_id,
            "confirmed": True,
            "native": True,
        }
    )
    assert not target.exists()
    assert other.exists()
    assert terminal_id not in supervisor.terminals.entries


@pytest.mark.asyncio
async def test_group_header_shows_count_first_with_a_compact_menu_on_the_same_line(
    supervisor, monkeypatch
):
    from kanbeat.ui.sessions import GroupActions, GroupForm

    group_id = (await supervisor.dispatch({"op": "group_create", "name": "Lorem"}))[
        "group"
    ]
    await supervisor.dispatch(
        {"op": "session_create", "title": "One", "group": group_id, "harness": "codex"}
    )
    app = KanbeatApp(supervisor.board.project)
    monkeypatch.setattr(app, "rpc", supervisor.dispatch)
    try:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause(0.4)
            section = app.query_one("#section-group-" + group_id)
            assert section.title == "[1]: Lorem"
            assert "[1]: Lorem" in str(section.query_one("CollapsibleTitle").render())
            button = app.query_one("#group-actions-" + group_id, Button)
            assert str(button.label) == "..."
            assert button.region.height == 1
            assert button.region.y == section.query_one("CollapsibleTitle").region.y
            row = app.query_one(".session-row")
            contents = section.query_one("Contents")
            assert row.region.right == contents.region.right - 2
            assert button.region.right == contents.region.right - 2
            await pilot.click(button)
            await pilot.pause()
            assert isinstance(app.screen, GroupActions)
            assert app.screen.query("#group-remove")
            await pilot.click("#group-rename")
            await pilot.pause()
            assert isinstance(app.screen, GroupForm)
    finally:
        await supervisor.terminals.stop()


@pytest.mark.asyncio
async def test_embedded_terminal_heading_is_brand_then_name_without_status(
    supervisor, monkeypatch
):
    from textual.widgets import Static

    opened = (
        await supervisor.dispatch(
            {"op": "session_create", "title": "CD", "harness": "codex"}
        )
    )["terminal"]
    app = KanbeatApp(supervisor.board.project)
    monkeypatch.setattr(app, "rpc", supervisor.dispatch)
    try:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.click("#open-session-" + opened)
            await pilot.pause(0.5)
            logo = app.query_one("#logo", Static)
            heading = app.query_one("#terminal-heading", Static)
            assert str(heading.render()) == "| CD"
            assert heading.region.y == logo.region.y
            assert heading.region.x > logo.region.x
    finally:
        await supervisor.terminals.stop()


@pytest.mark.asyncio
async def test_buttons_are_single_row_including_modal_close(supervisor, monkeypatch):
    from kanbeat.ui.app import NewTicket

    app = KanbeatApp(supervisor.board.project)
    monkeypatch.setattr(app, "rpc", supervisor.dispatch)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        assert app.query_one("#nav").region.height == 2
        for button in app.query("#nav Button"):
            if button.region.width:
                assert button.region.height == 1
        for button in app.query("#sessions-footer Button"):
            assert button.region.height == 1
        app.push_screen(NewTicket())
        await pilot.pause()
        close = app.screen.query_one("#close", Button)
        assert close.region.height == 1
        assert close.region.width <= 3
        assert app.screen.query_one("#save", Button).region.height == 1


@pytest.mark.asyncio
async def test_fixed_width_menu_buttons_render_exactly_their_width(
    supervisor, monkeypatch
):
    await supervisor.dispatch(
        {"op": "session_create", "title": "One", "harness": "codex"}
    )
    group_id = (await supervisor.dispatch({"op": "group_create", "name": "G"}))["group"]
    app = KanbeatApp(supervisor.board.project)
    monkeypatch.setattr(app, "rpc", supervisor.dispatch)
    try:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause(0.4)
            menus = list(app.query(".card-menu")) + [
                app.query_one("#group-actions-" + group_id)
            ]
            assert len(menus) >= 2
            for button in menus:
                width = button.region.width
                strips = button.render_lines(Region(0, 0, width, 1))
                assert [cell_len(strip.text) for strip in strips] == [width]
    finally:
        await supervisor.terminals.stop()


@pytest.mark.asyncio
async def test_new_session_can_be_a_plain_terminal(supervisor, monkeypatch):
    from kanbeat.ui.sessions import SessionEdit

    app = KanbeatApp(supervisor.board.project)
    monkeypatch.setattr(app, "rpc", supervisor.dispatch)
    try:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause(0.3)
            await pilot.click("#new-session")
            await pilot.pause(0.3)
            assert app.screen.query_one("#session-agent-fields").display
            assert app.screen.query_one("#open-harness").display
            await pilot.click("#session-title")
            await pilot.press(*"Work")
            app.screen.query_one("#session-harness", Select).value = "shell"
            await pilot.pause(0.3)
            assert not app.screen.query_one("#session-agent-fields").display
            assert not app.screen.query_one("#open-harness").display
            await pilot.click("#session-start")
            await pilot.pause(0.8)
            entry = next(iter(supervisor.terminals.entries.values()))
            assert entry["harness"] == "shell"
            assert not entry.get("native_session") and not entry.get("prompt")
            row = app.query_one("#open-session-" + entry["id"], Button)
            assert str(row.label) == "Work / terminal"
            await pilot.click("#session-actions-" + entry["id"])
            await pilot.pause(0.3)
            assert not app.screen.query("#session-remove-all")
            await pilot.click("#session-edit")
            await pilot.pause(0.3)
            assert isinstance(app.screen, SessionEdit)
            assert not app.screen.query("#session-native-id")
    finally:
        await supervisor.terminals.stop()

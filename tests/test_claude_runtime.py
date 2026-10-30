import asyncio
from pathlib import Path
import sys

import pytest

from kanbeat.claude_runtime import ClaudeRun
from kanbeat.native_events import record_event
from kanbeat.native_settings import NativeSettings
from kanbeat.supervisor import Supervisor


class FakeClaudeRuntime:
    def __init__(self, root: Path):
        self.root = root
        self.events_dir = root / "events"
        self.events_dir.mkdir(exist_ok=True)
        self.poll_interval = 0.005
        self.launches = []
        self.stopped = []
        self.tracked = set()
        self.events_id = None
        self._config_states = {}
        self.session = "session-1"
        self.rows = []
        self.mode = "ok"
        self.found = 0
        self.vanish_after = None
        self.configs = 0

    def child_env(self):
        return {"PATH": "/usr/bin"}

    def settings_args(self, events_id):
        self.events_id = events_id
        return ["--settings", "{}"]

    async def launch(self, args, *, timeout=30):
        self.launches.append(list(args))
        if "--bg" not in args:
            return (0, "", "")
        if "--resume" in args:
            self.session = args[args.index("--resume") + 1]
        assert self.events_id is not None
        if self.mode == "ok":
            record_event(
                "hook",
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": self.session,
                },
                self.events_dir,
                self.events_id,
            )
            record_event(
                "hook",
                {
                    "hook_event_name": "Stop",
                    "session_id": self.session,
                    "last_assistant_message": "KANBEAT_VERDICT: approve",
                    "stop_hook_active": False,
                },
                self.events_dir,
                self.events_id,
            )
            self.rows = [
                {"status": "busy", "state": "working"},
                {"status": "idle", "state": "working"},
            ]
        elif self.mode == "failure":
            record_event(
                "hook",
                {
                    "hook_event_name": "StopFailure",
                    "session_id": self.session,
                    "last_assistant_message": "Not logged in",
                    "error": "authentication_failed",
                },
                self.events_dir,
                self.events_id,
            )
            self.rows = [
                {"status": "busy", "state": "working"},
                {"status": "idle", "state": "blocked"},
            ]
        elif self.mode == "no-completion":
            self.rows = [{"status": "idle", "state": "blocked"}]
        elif self.mode == "hang":
            self.rows = [{"status": "busy", "state": "working"}]
        elif self.mode == "prompt-idle":
            record_event(
                "hook",
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": self.session,
                },
                self.events_dir,
                self.events_id,
            )
            self.rows = [{"status": "idle", "state": "working"}]
        elif self.mode == "failed-row":
            self.rows = [
                {"status": "busy", "state": "failed"},
                {"status": "idle", "state": "failed"},
            ]
        elif self.mode == "failed-stop":
            record_event(
                "hook",
                {
                    "hook_event_name": "Stop",
                    "session_id": self.session,
                    "last_assistant_message": "done",
                },
                self.events_dir,
                self.events_id,
            )
            self.rows = [
                {"status": "busy", "state": "failed"},
                {"status": "idle", "state": "failed"},
            ]
        elif self.mode == "stop-vanish":
            record_event(
                "hook",
                {
                    "hook_event_name": "Stop",
                    "session_id": self.session,
                    "last_assistant_message": "done",
                },
                self.events_dir,
                self.events_id,
            )
            self.rows = [{"status": "busy", "state": "working"}]
            self.vanish_after = 1
        elif self.mode == "stop-end":
            record_event(
                "hook",
                {
                    "hook_event_name": "Stop",
                    "session_id": self.session,
                    "last_assistant_message": "done",
                },
                self.events_dir,
                self.events_id,
            )
            record_event(
                "hook",
                {"hook_event_name": "SessionEnd", "session_id": self.session},
                self.events_dir,
                self.events_id,
            )
            self.rows = [{"status": "busy", "state": "working"}]
        return (0, "", "")

    async def launch_background(self, args, *, timeout=60):
        await self.launch(args)
        return (0, "backgrounded · abc12345\n")

    @staticmethod
    def background_id(output):
        return "abc12345" if "abc12345" in output else None

    async def wait_short(self, short, timeout):
        return {
            "sessionId": self.session,
            "id": short,
            "status": "idle",
            "state": "working",
        }

    async def find(self, session):
        if not self.rows:
            return None
        if self.vanish_after is not None and self.found >= self.vanish_after:
            return None
        self.found += 1
        index = min(self.found - 1, len(self.rows) - 1)
        return {"sessionId": self.session, "id": "abc12345", **self.rows[index]}

    @staticmethod
    def activity(row):
        if row is None:
            return None
        if row.get("state") == "blocked":
            return "waiting"
        if row.get("status") == "busy":
            return "working"
        if row.get("status") == "idle":
            return "idle"
        return None

    async def stop_session(self, session):
        self.stopped.append(session)
        self.rows = []
        return True

    def track(self, session):
        self.tracked.add(session)

    def untrack(self, session):
        self.tracked.discard(session)

    def config_state(self, events_id):
        from kanbeat.claude_runtime import ClaudeRuntime

        return ClaudeRuntime.config_state(self, events_id)

    def remove_events(self, events_id):
        return None

    async def start_config(self, events_id):
        self.events_id = events_id
        self.configs += 1
        self.session = f"session-{self.configs}"
        self.rows = [{"status": "idle", "state": "working"}]
        return self.session

    async def attach_argv(self, session, events_id):
        return [sys.executable, "-u", "-c", "import time; time.sleep(30)"]


@pytest.mark.asyncio
async def test_new_claude_run_uses_settings_and_waits_for_completion(tmp_path):
    runtime = FakeClaudeRuntime(tmp_path)
    activities = []
    sessions = []
    run = ClaudeRun(
        runtime,
        "do work",
        None,
        tmp_path / "run.log",
        settings=NativeSettings("claude", "claude-x", None, "high"),
        on_session=sessions.append,
        on_activity=activities.append,
    )
    outcome = await asyncio.wait_for(run.execute(), timeout=5)
    assert outcome.ok
    assert outcome.answer == "KANBEAT_VERDICT: approve"
    assert sessions == [run.session_id]
    args = runtime.launches[0]
    assert "--session-id" not in args
    assert args[-6:] == ["--model", "claude-x", "--effort", "high", "--", "do work"]
    assert activities[:3] == ["working", "idle", None]


@pytest.mark.asyncio
async def test_resumed_claude_run_keeps_its_own_settings(tmp_path):
    runtime = FakeClaudeRuntime(tmp_path)
    runtime.rows = [{"status": "busy", "state": "working"}]
    run = ClaudeRun(
        runtime,
        "continue",
        "session-1",
        tmp_path / "run.log",
        settings=NativeSettings("claude", "template", None, "high"),
    )
    original_find = runtime.find

    async def find_after_launch(session):
        row = await original_find(session)
        return row

    runtime.find = find_after_launch
    task = asyncio.create_task(run.execute())
    for _ in range(100):
        if runtime.events_id and runtime.launches:
            break
        await asyncio.sleep(0.01)
    runtime.rows = [
        {"status": "busy", "state": "working"},
        {"status": "idle", "state": "working"},
    ]
    record_event(
        "hook",
        {
            "hook_event_name": "Stop",
            "session_id": "session-1",
            "last_assistant_message": "done",
        },
        runtime.events_dir,
        runtime.events_id,
    )
    outcome = await asyncio.wait_for(task, timeout=5)
    assert outcome.ok
    assert "--resume" in runtime.launches[0]
    assert "--model" not in runtime.launches[0]
    assert "--effort" not in runtime.launches[0]


@pytest.mark.asyncio
async def test_stop_is_accepted_when_the_session_disappears(tmp_path, monkeypatch):
    import kanbeat.claude_runtime as module

    runtime = FakeClaudeRuntime(tmp_path)
    runtime.mode = "stop-vanish"
    monkeypatch.setattr(module, "STARTUP_GRACE", 0.01)
    run = ClaudeRun(runtime, "work", None, tmp_path / "run.log")
    outcome = await asyncio.wait_for(run.execute(), timeout=5)
    assert outcome.ok
    assert outcome.answer == "done"


@pytest.mark.asyncio
async def test_previous_failed_state_does_not_override_a_stop(tmp_path):
    runtime = FakeClaudeRuntime(tmp_path)
    runtime.mode = "failed-stop"
    run = ClaudeRun(runtime, "work", None, tmp_path / "run.log")
    outcome = await asyncio.wait_for(run.execute(), timeout=5)
    assert outcome.ok
    assert outcome.answer == "done"


@pytest.mark.asyncio
async def test_session_end_after_stop_is_success(tmp_path):
    runtime = FakeClaudeRuntime(tmp_path)
    runtime.mode = "stop-end"
    run = ClaudeRun(runtime, "work", None, tmp_path / "run.log")
    outcome = await asyncio.wait_for(run.execute(), timeout=5)
    assert outcome.ok
    assert outcome.answer == "done"


@pytest.mark.asyncio
async def test_prompt_hook_alone_does_not_trigger_the_short_idle_grace(
    tmp_path, monkeypatch
):
    import kanbeat.claude_runtime as module

    runtime = FakeClaudeRuntime(tmp_path)
    runtime.mode = "prompt-idle"
    monkeypatch.setattr(module, "IDLE_GRACE", 0.01)
    monkeypatch.setattr(module, "STARTUP_GRACE", 0.05)
    run = ClaudeRun(runtime, "work", None, tmp_path / "run.log")
    outcome = await asyncio.wait_for(run.execute(), timeout=5)
    assert not outcome.ok
    assert "did not start" in outcome.reason


@pytest.mark.asyncio
async def test_failure_without_idle_breaks_after_grace(tmp_path, monkeypatch):
    import kanbeat.claude_runtime as module

    runtime = FakeClaudeRuntime(tmp_path)
    runtime.mode = "failed-row"
    monkeypatch.setattr(module, "FAILURE_GRACE", 0.01)
    run = ClaudeRun(runtime, "work", None, tmp_path / "run.log")
    outcome = await asyncio.wait_for(run.execute(), timeout=5)
    assert not outcome.ok
    assert "failed" in outcome.reason.lower()


@pytest.mark.asyncio
async def test_claude_failure_and_stop_are_reported(tmp_path):
    runtime = FakeClaudeRuntime(tmp_path)
    runtime.mode = "failure"
    run = ClaudeRun(runtime, "work", None, tmp_path / "run.log")
    outcome = await asyncio.wait_for(run.execute(), timeout=5)
    assert not outcome.ok
    assert (
        "authentication_failed" in outcome.reason or "Not logged in" in outcome.reason
    )

    runtime = FakeClaudeRuntime(tmp_path)
    runtime.mode = "hang"
    run = ClaudeRun(runtime, "long", None, tmp_path / "run2.log")
    task = asyncio.create_task(run.execute())
    for _ in range(100):
        if run.session_id and runtime.launches:
            break
        await asyncio.sleep(0.01)
    await run.stop()
    outcome = await asyncio.wait_for(task, timeout=5)
    assert not outcome.ok
    assert "Stop" in outcome.reason
    assert runtime.stopped and set(runtime.stopped) == {run.session_id}


@pytest.mark.asyncio
async def test_resume_stops_a_running_session_before_taking_over(tmp_path):
    runtime = FakeClaudeRuntime(tmp_path)
    runtime.rows = [{"status": "busy", "state": "working"}]
    run = ClaudeRun(runtime, "continue", "session-1", tmp_path / "run.log")
    original_stop = runtime.stop_session
    stopped = []

    async def stop_once(session):
        stopped.append(session)
        return await original_stop(session)

    runtime.stop_session = stop_once
    outcome = await asyncio.wait_for(run.execute(), timeout=5)
    assert outcome.ok
    assert stopped[0] == "session-1"
    assert runtime.launches[0][:3] == ["--bg", "--resume", "session-1"]


@pytest.mark.asyncio
async def test_recover_orphans_stops_recorded_claude_sessions(tmp_path):
    runtime = FakeClaudeRuntime(tmp_path)
    supervisor = Supervisor(tmp_path)
    supervisor.claude = runtime
    ticket = supervisor.board.create("Task", column="progress")
    state = supervisor.ticket_state(ticket.id)
    state["developer"] = {"harness": "claude", "session": "session-1"}
    state["reviewers"] = [
        {"id": "reviewer-1", "harness": "codex", "session": "thread-1"}
    ]
    supervisor.terminals.entries["cfg"] = {
        "id": "cfg",
        "owner": "custom",
        "role": "config",
        "harness": "claude",
        "native_session": "session-2",
    }
    await supervisor.recover_orphans()
    assert set(runtime.stopped) == {"session-1", "session-2"}
    assert "thread-1" not in runtime.stopped


def test_event_files_are_removed_and_old_files_are_swept(tmp_path):
    from kanbeat.claude_runtime import ClaudeRuntime

    runtime = ClaudeRuntime(tmp_path, tmp_path / "state")
    runtime.events_dir.mkdir(parents=True)
    old = runtime.events_dir / "old.jsonl"
    old.write_text("{}")
    old_sidecar = runtime.events_dir / "old.settings.json"
    old_sidecar.write_text("{}")
    keep = runtime.events_dir / "keep.jsonl"
    keep.write_text("{}")
    stamp = 1
    import os

    os.utime(old, (stamp, stamp))
    os.utime(old_sidecar, (stamp, stamp))
    runtime.sweep_events(max_age=60)
    assert not old.exists() and not old_sidecar.exists()
    assert keep.exists()
    runtime.remove_events("keep")
    assert not keep.exists()


def test_session_missing_detection():
    from kanbeat.claude_runtime import ClaudeSessionMissing, session_missing

    assert session_missing("No conversation found with session ID: abc")
    assert session_missing("session abc does not exist")
    assert not session_missing("session is already running")
    assert issubclass(ClaudeSessionMissing, Exception)


@pytest.mark.asyncio
async def test_stale_claude_config_session_is_replaced(tmp_path, monkeypatch):
    monkeypatch.setattr("kanbeat.session_groups.available", lambda name: True)
    runtime = FakeClaudeRuntime(tmp_path)
    supervisor = Supervisor(tmp_path)
    supervisor.claude = runtime
    supervisor.session_groups.claude_runtime = runtime
    try:
        first = await supervisor.dispatch(
            {"op": "session_config_open", "harness": "claude"}
        )
        entry = supervisor.terminals.entries[first["terminal"]]
        assert entry["native_session"] == "session-1"
        await supervisor.terminals.stop()

        from kanbeat.claude_runtime import ClaudeSessionMissing

        calls = {"count": 0}
        original = runtime.attach_argv

        async def stale_once(session, events_id):
            if calls["count"] == 0:
                calls["count"] += 1
                raise ClaudeSessionMissing("session no longer exists")
            return await original(session, events_id)

        runtime.attach_argv = stale_once
        reopened = await supervisor.dispatch(
            {"op": "session_config_open", "harness": "claude"}
        )
        assert reopened["terminal"] == first["terminal"]
        assert entry["native_session"] == "session-2"
        assert calls["count"] == 1
    finally:
        await supervisor.terminals.stop()


@pytest.mark.asyncio
async def test_stale_claude_task_session_is_forgotten(tmp_path):
    runtime = FakeClaudeRuntime(tmp_path)

    async def stale(session, events_id):
        from kanbeat.claude_runtime import ClaudeSessionMissing

        raise ClaudeSessionMissing("session no longer exists")

    runtime.attach_argv = stale
    supervisor = Supervisor(tmp_path)
    supervisor.claude = runtime
    ticket = supervisor.board.create("Task", column="progress")
    state = supervisor.ticket_state(ticket.id)
    state["developer"] = {"harness": "claude", "session": "missing"}
    state["runtime"]["developer"] = {"status": "running", "harness": "claude"}
    with pytest.raises(ValueError, match="no longer available"):
        await supervisor.dispatch({"op": "terminal_attach", "ticket": ticket.id})
    assert "session" not in state["developer"]


def test_config_state_maps_hook_activity(tmp_path):
    runtime = FakeClaudeRuntime(tmp_path)
    events_id = "activity"
    expected = [
        ("UserPromptSubmit", "working"),
        ("Notification", "waiting"),
        ("PreToolUse", "working"),
        ("Stop", "idle"),
        ("StopFailure", "error"),
    ]
    for event, activity in expected:
        record_event(
            "hook",
            {"hook_event_name": event, "session_id": "session-1"},
            runtime.events_dir,
            events_id,
        )
        _settings, current = runtime.config_state(events_id)
        assert current == activity


@pytest.mark.asyncio
async def test_claude_config_session_opens_and_captures_settings(tmp_path, monkeypatch):
    monkeypatch.setattr("kanbeat.session_groups.available", lambda name: True)
    runtime = FakeClaudeRuntime(tmp_path)
    supervisor = Supervisor(tmp_path)
    supervisor.claude = runtime
    supervisor.session_groups.claude_runtime = runtime
    supervisor.runtime.config["default_harness"] = "claude"
    try:
        result = await supervisor.dispatch(
            {"op": "session_config_open", "harness": "claude"}
        )
        entry = supervisor.terminals.entries[result["terminal"]]
        assert entry["role"] == "config"
        assert entry["native_session"] == "session-1"
        assert entry["events_id"]
        record_event(
            "statusline",
            {
                "session_id": "session-1",
                "model": {"id": "claude-x"},
                "effort": {"level": "high"},
            },
            runtime.events_dir,
            entry["events_id"],
        )
        await supervisor.sync_claude_config()
        assert supervisor.runtime.data["native_settings"]["claude"] == {
            "model": "claude-x",
            "effort": "high",
        }
        again = await supervisor.dispatch(
            {"op": "session_config_open", "harness": "claude"}
        )
        assert again["terminal"] == result["terminal"]
    finally:
        await supervisor.terminals.stop()

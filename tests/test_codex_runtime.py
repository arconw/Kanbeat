import asyncio
import sys
from types import SimpleNamespace

import pytest

from kanbeat.codex_host import (
    CodexNotification,
    CodexRpcError,
    CodexTimeout,
    thread_missing,
)
from kanbeat.codex_runtime import CodexRun
from kanbeat.native_settings import NativeSettings
from kanbeat.supervisor import Supervisor


class FakeSubscription:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.closed = False

    async def get(self, timeout=None):
        try:
            return await asyncio.wait_for(self.queue.get(), timeout)
        except asyncio.TimeoutError as error:
            raise CodexTimeout("empty") from error

    def push(self, method, params):
        self.queue.put_nowait(CodexNotification(method, params, params.get("threadId")))

    def close(self):
        self.closed = True
        self.queue.put_nowait(CodexNotification("__closed__", {}, None))


class FakeCodexHost:
    def __init__(self):
        self.running = True
        self.threads = {}
        self.started_threads = []
        self.turns = []
        self.observed = []
        self.interrupted = []
        self.subscription = None
        self.settings = NativeSettings("codex", "gpt-x", "openai", "high")

    async def start_thread(self, cwd, *, model=None, model_provider=None):
        self.started_threads.append((cwd, model, model_provider))
        thread_id = "01a18660-9ca4-71c0-ba25-81426f4ca65d"
        self.threads[thread_id] = {
            "id": thread_id,
            "model": model or "gpt-x",
            "modelProvider": model_provider or "openai",
            "reasoningEffort": self.settings.effort,
            "status": {"type": "idle"},
            "cwd": str(cwd),
        }
        return SimpleNamespace(thread_id=thread_id)

    async def observe_thread(self, session):
        self.observed.append(session)
        if session not in self.threads:
            raise CodexRpcError(-32600, "thread not found")

    async def start_turn(self, session, text, *, model=None, effort=None):
        self.turns.append((session, text, model, effort))
        turn_id = "turn-1"
        subscription = self.subscription
        assert subscription is not None
        subscription.push(
            "turn/started",
            {
                "threadId": session,
                "turn": {"id": turn_id, "status": "inProgress", "items": []},
            },
        )
        subscription.push(
            "item/started",
            {
                "threadId": session,
                "turnId": turn_id,
                "item": {"id": "item-1", "type": "commandExecution"},
            },
        )
        subscription.push(
            "item/completed",
            {
                "threadId": session,
                "turnId": turn_id,
                "item": {
                    "id": "item-2",
                    "type": "agentMessage",
                    "text": "KANBEAT_VERDICT: approve",
                },
            },
        )
        subscription.push(
            "turn/completed",
            {
                "threadId": session,
                "turn": {
                    "id": turn_id,
                    "status": "completed",
                    "items": [
                        {
                            "id": "item-2",
                            "type": "agentMessage",
                            "text": "KANBEAT_VERDICT: approve",
                        }
                    ],
                },
            },
        )
        return {"id": turn_id}

    async def interrupt_turn(self, session, turn_id):
        self.interrupted.append((session, turn_id))
        if self.subscription is not None:
            self.subscription.push(
                "turn/completed",
                {
                    "threadId": session,
                    "turn": {"id": turn_id, "status": "interrupted", "items": []},
                },
            )

    def subscribe(self, session):
        self.subscription = FakeSubscription()
        return self.subscription

    async def read_thread(self, session):
        if session not in self.threads:
            raise CodexRpcError(-32600, "thread not found")
        return SimpleNamespace(
            thread_id=session,
            model=self.threads[session]["model"],
            model_provider=self.threads[session]["modelProvider"],
            reasoning_effort=self.threads[session]["reasoningEffort"],
            status="idle",
            active_flags=(),
            to_dict=lambda: self.threads[session],
            cwd=self.threads[session]["cwd"],
        )

    def viewer_argv(self, session):
        return [sys.executable, "-u", "-c", "import time; time.sleep(30)"]

    def child_env(self):
        return {"PATH": "/usr/bin"}

    def tui_argv(self, *, thread_id=None, prompt=None):
        return self.viewer_argv(thread_id)

    async def set_thread_name(self, session, name):
        self.threads[session]["name"] = name

    async def close(self):
        self.running = False


@pytest.fixture
def codex_supervisor(tmp_path, monkeypatch):
    supervisor = Supervisor(tmp_path)
    host = FakeCodexHost()
    supervisor.codex = host

    async def fake_host():
        return host

    monkeypatch.setattr(supervisor, "codex_host", fake_host)
    monkeypatch.setattr("kanbeat.supervisor.available", lambda name: True)
    monkeypatch.setattr("kanbeat.session_groups.available", lambda name: True)
    return supervisor, host


def test_thread_missing_requires_a_real_app_server_error():
    thread = "01a18660-9ca4-71c0-ba25-81426f4ca65d"
    assert not thread_missing(CodexRpcError(-32600, "thread not loaded: " + thread))
    assert thread_missing(
        CodexRpcError(-32600, "no rollout found for thread id " + thread)
    )
    assert not thread_missing(CodexRpcError(-32600, "unknown model in session config"))
    assert not thread_missing(CodexRpcError(-32600, "thread is missing a field"))
    assert not thread_missing("session not found")


@pytest.mark.asyncio
async def test_new_codex_thread_uses_settings_and_reports_events(tmp_path):
    host = FakeCodexHost()
    activities = []
    sessions = []
    run = CodexRun(
        host,
        "do work",
        None,
        tmp_path,
        tmp_path / "run.log",
        settings=NativeSettings("codex", "gpt-x", "openai", "high"),
        on_session=sessions.append,
        on_activity=activities.append,
    )
    outcome = await asyncio.wait_for(run.execute(), timeout=5)
    assert outcome.ok
    assert outcome.answer == "KANBEAT_VERDICT: approve"
    assert sessions == ["01a18660-9ca4-71c0-ba25-81426f4ca65d"]
    assert host.started_threads == [(tmp_path, "gpt-x", "openai")]
    assert host.turns[0][2:] == ("gpt-x", "high")
    assert activities[:3] == ["working", "idle", None]
    assert "KANBEAT_VERDICT: approve" in (tmp_path / "run.log").read_text()


@pytest.mark.asyncio
async def test_resume_keeps_native_settings_and_detects_missing_thread(tmp_path):
    host = FakeCodexHost()
    thread = "01a18660-9ca4-71c0-ba25-81426f4ca65d"
    host.threads[thread] = {
        "id": thread,
        "model": "own-model",
        "modelProvider": "own-provider",
        "reasoningEffort": "low",
        "status": {"type": "idle"},
        "cwd": str(tmp_path),
    }
    run = CodexRun(
        host,
        "continue",
        thread,
        tmp_path,
        tmp_path / "run.log",
        settings=NativeSettings("codex", "template", "template", "high"),
    )
    outcome = await asyncio.wait_for(run.execute(), timeout=5)
    assert outcome.ok
    assert host.observed == [thread]
    assert host.turns[0][2:] == (None, None)

    missing = CodexRun(
        host,
        "continue",
        "01a18660-0000-7000-8000-000000000000",
        tmp_path,
        tmp_path / "missing.log",
    )
    outcome = await asyncio.wait_for(missing.execute(), timeout=5)
    assert not outcome.ok
    assert outcome.resume_missing


@pytest.mark.asyncio
async def test_missing_turn_id_fails_fast(tmp_path):
    host = FakeCodexHost()

    async def empty_turn(session, text, *, model=None, effort=None):
        host.turns.append((session, text, model, effort))
        return {"id": ""}

    host.start_turn = empty_turn
    run = CodexRun(host, "work", None, tmp_path, tmp_path / "run.log")
    outcome = await asyncio.wait_for(run.execute(), timeout=5)
    assert not outcome.ok
    assert "no turn id" in outcome.reason
    assert run.subscription is None


@pytest.mark.asyncio
async def test_stop_after_notification_does_not_use_a_closed_subscription(tmp_path):
    host = FakeCodexHost()
    run = CodexRun(host, "work", None, tmp_path, tmp_path / "run.log")

    async def stop_after_started(session, text, *, model=None, effort=None):
        host.turns.append((session, text, model, effort))
        host.subscription.push(
            "turn/started",
            {
                "threadId": session,
                "turn": {"id": "turn-1", "status": "inProgress", "items": []},
            },
        )
        await run.stop()
        return {"id": "turn-1"}

    host.start_turn = stop_after_started
    outcome = await asyncio.wait_for(run.execute(), timeout=5)
    assert not outcome.ok
    assert "interrupted" in outcome.reason.lower()


@pytest.mark.asyncio
async def test_retryable_error_does_not_poison_completed_outcome(tmp_path):
    host = FakeCodexHost()

    async def retry_then_complete(session, text, *, model=None, effort=None):
        host.turns.append((session, text, model, effort))
        host.subscription.push(
            "turn/started",
            {
                "threadId": session,
                "turn": {"id": "turn-1", "status": "inProgress", "items": []},
            },
        )
        host.subscription.push(
            "error",
            {
                "threadId": session,
                "turnId": "turn-1",
                "error": {"message": "temporary overload"},
                "willRetry": True,
            },
        )
        host.subscription.push(
            "turn/completed",
            {
                "threadId": session,
                "turn": {"id": "turn-1", "status": "completed", "items": []},
            },
        )
        return {"id": "turn-1"}

    host.start_turn = retry_then_complete
    run = CodexRun(host, "work", None, tmp_path, tmp_path / "run.log")
    outcome = await asyncio.wait_for(run.execute(), timeout=5)
    assert outcome.ok
    assert outcome.reason == "completed"


@pytest.mark.asyncio
async def test_failed_turn_without_completion_returns_error(tmp_path, monkeypatch):
    import kanbeat.codex_runtime as module

    host = FakeCodexHost()

    async def fail_without_completion(session, text, *, model=None, effort=None):
        host.subscription.push(
            "turn/started",
            {
                "threadId": session,
                "turn": {"id": "turn-1", "status": "inProgress", "items": []},
            },
        )
        host.subscription.push(
            "error",
            {
                "threadId": session,
                "turnId": "turn-1",
                "error": {"message": "fatal transport failure"},
                "willRetry": False,
            },
        )
        return {"id": "turn-1"}

    host.start_turn = fail_without_completion
    monkeypatch.setattr(module, "FAILURE_GRACE", 0.01)
    run = CodexRun(host, "work", None, tmp_path, tmp_path / "run.log")
    outcome = await asyncio.wait_for(run.execute(), timeout=5)
    assert not outcome.ok
    assert "fatal transport failure" in outcome.reason


@pytest.mark.asyncio
async def test_stop_interrupts_turn_and_returns_interrupted(tmp_path):
    host = FakeCodexHost()

    async def slow_start(session, text, *, model=None, effort=None):
        host.turns.append((session, text, model, effort))
        host.subscription.push(
            "turn/started",
            {
                "threadId": session,
                "turn": {"id": "turn-1", "status": "inProgress", "items": []},
            },
        )
        return {"id": "turn-1"}

    host.start_turn = slow_start
    run = CodexRun(host, "long", None, tmp_path, tmp_path / "run.log")
    task = asyncio.create_task(run.execute())
    for _ in range(100):
        if run.turn_id is not None:
            break
        await asyncio.sleep(0.01)
    await run.stop()
    outcome = await asyncio.wait_for(task, timeout=5)
    assert not outcome.ok
    assert "Stop" in outcome.reason
    assert host.interrupted == [(run.session_id, "turn-1")]


@pytest.mark.asyncio
async def test_codex_config_session_opens_and_updates_settings(codex_supervisor):
    supervisor, host = codex_supervisor
    result = await supervisor.dispatch(
        {"op": "session_config_open", "harness": "codex"}
    )
    terminal = supervisor.terminals.entries[result["terminal"]]
    try:
        assert terminal["role"] == "config"
        assert terminal["native_session"] in host.threads
        assert host.threads[terminal["native_session"]]["name"] == "Kanbeat config"
        await supervisor.sync_codex_config()
        assert supervisor.runtime.data["native_settings"]["codex"] == {
            "model": "gpt-x",
            "provider": "openai",
            "effort": "high",
        }
        again = await supervisor.dispatch(
            {"op": "session_config_open", "harness": "codex"}
        )
        assert again["terminal"] == result["terminal"]
    finally:
        await supervisor.terminals.stop()


@pytest.mark.asyncio
async def test_invalid_config_metadata_does_not_erase_saved_settings(codex_supervisor):
    supervisor, host = codex_supervisor
    supervisor.runtime.data["native_settings"]["codex"] = {
        "model": "saved-model",
        "provider": "saved-provider",
        "effort": "low",
    }
    supervisor.terminals.entries["cfg"] = {
        "id": "cfg",
        "owner": "custom",
        "role": "config",
        "harness": "codex",
        "native_session": "01a18660-9ca4-71c0-ba25-81426f4ca65d",
        "status": "running",
    }
    supervisor.terminals.sessions["cfg"] = object()

    async def invalid_metadata(session):
        return SimpleNamespace(
            status="idle",
            active_flags=(),
            to_dict=lambda: {
                "model": "bad value",
                "modelProvider": "openai",
                "reasoningEffort": "none",
            },
        )

    host.read_thread = invalid_metadata
    await supervisor.sync_codex_config()
    assert supervisor.runtime.data["native_settings"]["codex"] == {
        "model": "saved-model",
        "provider": "saved-provider",
        "effort": "low",
    }


@pytest.mark.asyncio
async def test_attach_missing_thread_does_not_drop_active_session(codex_supervisor):
    supervisor, _host = codex_supervisor
    ticket = supervisor.board.create("Task", column="progress")
    state = supervisor.ticket_state(ticket.id)
    state["developer"] = {
        "harness": "codex",
        "session": "01a18660-0000-7000-8000-000000000000",
    }
    state["runtime"]["developer"] = {"status": "running", "harness": "codex"}
    job = asyncio.create_task(asyncio.sleep(10))
    supervisor.jobs[supervisor.slot_key(ticket.id, "developer")] = job
    try:
        with pytest.raises(ValueError, match="still starting"):
            await supervisor.dispatch({"op": "terminal_attach", "ticket": ticket.id})
        assert state["developer"]["session"] == ("01a18660-0000-7000-8000-000000000000")
    finally:
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)


def test_invalid_stored_codex_settings_are_kept_and_reported(codex_supervisor):
    supervisor, _host = codex_supervisor
    supervisor.runtime.data["native_settings"]["codex"] = {"model": "bad value"}
    assert supervisor.native_settings("codex") is None
    assert supervisor.runtime.data["native_settings"]["codex"] == {"model": "bad value"}
    assert supervisor.runtime.data["notices"][-1]["level"] == "warning"
    assert "invalid" in supervisor.runtime.data["notices"][-1]["message"].lower()


@pytest.mark.asyncio
async def test_codex_config_sync_backs_off_after_timeout(codex_supervisor):
    supervisor, host = codex_supervisor
    supervisor.terminals.entries["cfg"] = {
        "id": "cfg",
        "owner": "custom",
        "role": "config",
        "harness": "codex",
        "native_session": "01a18660-9ca4-71c0-ba25-81426f4ca65d",
        "status": "running",
    }
    supervisor.terminals.sessions["cfg"] = object()
    calls = 0

    async def timeout(session):
        nonlocal calls
        calls += 1
        raise asyncio.TimeoutError

    host.read_thread = timeout
    await supervisor.sync_codex_config()
    await supervisor.sync_codex_config()
    assert calls == 1
    assert supervisor._codex_sync_backoff > 0


@pytest.mark.asyncio
async def test_codex_config_activity_uses_thread_status(codex_supervisor):
    supervisor, host = codex_supervisor
    supervisor.terminals.entries["cfg"] = {
        "id": "cfg",
        "owner": "custom",
        "role": "config",
        "harness": "codex",
        "native_session": "01a18660-9ca4-71c0-ba25-81426f4ca65d",
        "status": "running",
    }
    supervisor.terminals.sessions["cfg"] = object()

    async def status_reader(session):
        return SimpleNamespace(
            status="active",
            active_flags=("waitingOnApproval",),
            to_dict=lambda: {"model": "gpt-x", "modelProvider": "openai"},
        )

    host.read_thread = status_reader
    await supervisor.sync_codex_config()
    assert supervisor.terminals.entries["cfg"]["activity"] == "waiting"

    async def idle_reader(session):
        return SimpleNamespace(
            status="idle",
            active_flags=(),
            to_dict=lambda: {"model": "gpt-x", "modelProvider": "openai"},
        )

    host.read_thread = idle_reader
    await supervisor.sync_codex_config()
    assert supervisor.terminals.entries["cfg"]["activity"] == "idle"


@pytest.mark.asyncio
async def test_codex_missing_inactive_thread_is_forgotten(codex_supervisor):
    supervisor, _host = codex_supervisor
    ticket = supervisor.board.create("Task", column="progress")
    state = supervisor.ticket_state(ticket.id)
    state["developer"] = {
        "harness": "codex",
        "session": "01a18660-0000-7000-8000-000000000000",
    }
    with pytest.raises(ValueError, match="no longer available"):
        await supervisor.dispatch({"op": "terminal_attach", "ticket": ticket.id})
    assert "session" not in state["developer"]


@pytest.mark.asyncio
async def test_codex_reviewer_slots_attach_independently(codex_supervisor):
    supervisor, host = codex_supervisor
    ticket = supervisor.board.create("Review", column="review")
    state = supervisor.ticket_state(ticket.id)
    state["reviewers"] = [
        {
            "id": "reviewer-1",
            "harness": "codex",
            "session": "01a18660-1111-7111-8111-111111111111",
        },
        {
            "id": "reviewer-2",
            "harness": "codex",
            "session": "01a18660-2222-7222-8222-222222222222",
        },
    ]
    for row in state["reviewers"]:
        host.threads[row["session"]] = {
            "id": row["session"],
            "model": "gpt-x",
            "modelProvider": "openai",
            "reasoningEffort": "high",
            "status": {"type": "idle"},
            "cwd": str(supervisor.board.project),
        }
    try:
        first = await supervisor.dispatch(
            {"op": "terminal_attach", "ticket": ticket.id, "slot": "reviewer-1"}
        )
        second = await supervisor.dispatch(
            {"op": "terminal_attach", "ticket": ticket.id, "slot": "reviewer-2"}
        )
        assert first["terminal"] != second["terminal"]
        assert (
            supervisor.terminals.entries[first["terminal"]]["native_session"]
            != supervisor.terminals.entries[second["terminal"]]["native_session"]
        )
    finally:
        await supervisor.terminals.stop()


@pytest.mark.asyncio
async def test_codex_worker_attaches_live_and_is_read_only_while_running(
    codex_supervisor,
):
    supervisor, host = codex_supervisor
    ticket = supervisor.board.create("Task", column="progress")
    state = supervisor.ticket_state(ticket.id)
    state["phase"] = "progress"
    state["iteration"] = 1
    state["developer"]["harness"] = "codex"
    state["developer"]["session"] = "01a18660-9ca4-71c0-ba25-81426f4ca65d"
    host.threads[state["developer"]["session"]] = {
        "id": state["developer"]["session"],
        "model": "gpt-x",
        "modelProvider": "openai",
        "reasoningEffort": "high",
        "status": {"type": "idle"},
        "cwd": str(supervisor.board.project),
    }
    supervisor.runtime.data["execution"] = "running"
    try:
        attached = await supervisor.dispatch(
            {"op": "terminal_attach", "ticket": ticket.id}
        )
        terminal = supervisor.terminals.get(attached["terminal"])
        assert terminal.entry["owner"] == "attach"
        assert terminal.entry["native_session"] == state["developer"]["session"]
        assert supervisor.snapshot()["terminals"][attached["terminal"]]["attached"]

        job = asyncio.create_task(asyncio.sleep(10))
        supervisor.jobs[supervisor.slot_key(ticket.id, "developer")] = job
        try:
            snapshot = await supervisor.dispatch(
                {"op": "terminal_snapshot", "terminal": attached["terminal"]}
            )
            assert snapshot["read_only"] is True
            with pytest.raises(ValueError, match="live worker monitor"):
                await supervisor.dispatch(
                    {
                        "op": "terminal_input",
                        "terminal": attached["terminal"],
                        "kind": "paste",
                        "text": "x",
                    }
                )
        finally:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        snapshot = await supervisor.dispatch(
            {"op": "terminal_snapshot", "terminal": attached["terminal"]}
        )
        assert snapshot["read_only"] is False
    finally:
        await supervisor.terminals.stop()

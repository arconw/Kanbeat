import asyncio
import os
import sys

import pytest

from kanbeat.host_process import RuntimeIdentity
from kanbeat.native import process_identity
from kanbeat.opencode_host import (
    OpenCodeHostError,
    OpenCodeHttpError,
    OpenCodeModel,
    OpenCodeSessionMetadata,
)
from kanbeat.supervisor import Supervisor


WORKER = """
import json
for event in (
    {"type": "step_start"},
    {"type": "text", "sessionID": "ses_test", "part": {"text": "done"}},
    {"type": "step_finish", "part": {"reason": "stop"}},
):
    print(json.dumps(event), flush=True)
"""

VIEWER = """
print('LIVE READY', flush=True)
import time
time.sleep(30)
"""


class FakeHost:
    def __init__(self):
        self.running = True
        self.sessions = {}
        self.created = 0
        self.prompts = []
        self.models = []
        self.worker_models = []

    def child_env(self):
        return {"PATH": os.environ["PATH"]}

    async def session_status(self, session_id):
        return "idle"

    def worker_argv(self, session_id, *, prompt=None, model=None):
        self.prompts.append(prompt)
        self.worker_models.append(model)
        return [sys.executable, "-u", "-c", WORKER]

    def viewer_argv(self, session_id):
        return [sys.executable, "-u", "-c", VIEWER]

    async def read_session(self, session_id):
        if session_id not in self.sessions:
            raise OpenCodeHttpError(404)
        return self.sessions[session_id]

    async def create_session(self, *, directory=None, model=None):
        self.created += 1
        self.models.append(model)
        session_id = "ses_test"
        self.sessions[session_id] = OpenCodeSessionMetadata(session_id, None, None)
        return self.sessions[session_id]


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    supervisor = Supervisor(tmp_path)
    host = FakeHost()

    async def fake_host():
        return host

    monkeypatch.setattr(supervisor, "opencode_host", fake_host)
    monkeypatch.setattr("kanbeat.supervisor.available", lambda name: True)
    monkeypatch.setattr("kanbeat.session_groups.available", lambda name: True)
    return supervisor, host


async def wait_text(session, text):
    for _ in range(200):
        if text in session.snapshot()["text"]:
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"Missing terminal text {text!r}: {session.snapshot()['text']}")


@pytest.mark.asyncio
async def test_opencode_worker_uses_shared_session_and_attaches(runtime):
    supervisor, host = runtime
    ticket = supervisor.board.create("Task", column="progress")
    state = supervisor.ticket_state(ticket.id)
    state["phase"] = "progress"
    state["iteration"] = 1
    state["developer"]["harness"] = "opencode"
    state["developer"]["pending_handoff"] = "HANDOFF"
    supervisor.runtime.config["developer_prompt"] = "CUSTOM DEV"
    supervisor.terminals.entries["cfg"] = {
        "id": "cfg",
        "owner": "custom",
        "role": "config",
        "harness": "opencode",
        "native_session": "ses_config",
    }
    host.sessions["ses_config"] = OpenCodeSessionMetadata(
        "ses_config", None, OpenCodeModel("p", "m", "high")
    )
    supervisor.runtime.data["execution"] = "running"
    try:
        await supervisor.start_worker(ticket, "developer", state["developer"])
        await supervisor.jobs["001:developer"]
        assert host.created == 1
        assert state["developer"]["session"] == "ses_test"
        assert "CUSTOM DEV" in host.prompts[0]
        assert "HANDOFF" in host.prompts[0]
        assert "pending_handoff" not in state["developer"]
        assert host.models[0].variant == "high"
        assert host.worker_models[0].variant == "high"
        assert state["runtime"]["developer"]["status"] == "completed"
        assert state["runtime"]["developer"]["harness"] == "opencode"

        attached = await supervisor.dispatch(
            {"op": "terminal_attach", "ticket": ticket.id}
        )
        entry = supervisor.terminals.entries[attached["terminal"]]
        assert entry["owner"] == "attach"
        assert entry["native_session"] == "ses_test"
        await wait_text(supervisor.terminals.get(attached["terminal"]), "LIVE READY")
        again = await supervisor.dispatch(
            {"op": "terminal_attach", "ticket": ticket.id}
        )
        assert again == attached
        with pytest.raises(ValueError, match="Stop the board"):
            await supervisor.dispatch({"op": "terminal_open", "ticket": ticket.id})
        await supervisor.terminals.stop()
        with pytest.raises(ValueError, match="Reattach the live session"):
            await supervisor.terminals.restart(attached["terminal"])
    finally:
        await supervisor.terminals.stop()


@pytest.mark.asyncio
async def test_opencode_config_activity_uses_session_status(runtime, monkeypatch):
    supervisor, host = runtime
    supervisor.terminals.entries["cfg"] = {
        "id": "cfg",
        "owner": "custom",
        "role": "config",
        "harness": "opencode",
        "native_session": "ses_test",
        "status": "running",
    }
    supervisor.terminals.sessions["cfg"] = object()
    supervisor.opencode = host

    async def busy(session_id):
        return "busy"

    monkeypatch.setattr(host, "session_status", busy)
    await supervisor.sync_opencode_config()
    assert supervisor.terminals.entries["cfg"]["activity"] == "working"

    async def idle(session_id):
        return "idle"

    monkeypatch.setattr(host, "session_status", idle)
    await supervisor.sync_opencode_config()
    assert supervisor.terminals.entries["cfg"]["activity"] == "idle"


@pytest.mark.asyncio
async def test_recover_keeps_host_record_when_stop_fails(runtime, monkeypatch):
    supervisor, _host = runtime
    record = RuntimeIdentity(
        "opencode", 123456, "linux:1", "http://127.0.0.1:1"
    ).to_dict()
    supervisor.runtime.data["hosts"] = {"opencode": record}

    async def failed_stop(identity):
        return False

    monkeypatch.setattr("kanbeat.supervisor.stop_recorded", failed_stop)
    monkeypatch.setattr("kanbeat.supervisor.process_identity", lambda pid: "linux:1")
    await supervisor.recover_orphans()
    assert supervisor.runtime.data["hosts"]["opencode"] == record


@pytest.mark.asyncio
async def test_opencode_host_is_reused_and_closed(runtime, monkeypatch):
    supervisor, _host = runtime

    class FakeServe:
        instances = []

        def __init__(self, cwd, env=None):
            self.closed = False
            self.running = True
            self.instances.append(self)

        async def start(self):
            return RuntimeIdentity(
                "opencode", os.getpid(), "linux:1", "http://127.0.0.1:1"
            )

        async def close(self):
            self.closed = True
            self.running = False

    monkeypatch.delattr(supervisor, "opencode_host")
    monkeypatch.setattr("kanbeat.supervisor.OpenCodeServeHost", FakeServe)
    first = await supervisor.opencode_host()
    second = await supervisor.opencode_host()
    assert first is second
    assert supervisor.runtime.data["hosts"]["opencode"]["kind"] == "opencode"
    await supervisor.close_hosts()
    assert first.closed
    assert "opencode" not in supervisor.runtime.data["hosts"]


@pytest.mark.asyncio
async def test_recover_orphans_stops_recorded_host(runtime):
    supervisor, _host = runtime
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(60)",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    identity = RuntimeIdentity(
        "opencode", process.pid, process_identity(process.pid), "http://127.0.0.1:1"
    )
    supervisor.runtime.data["hosts"] = {"opencode": identity.to_dict()}
    try:
        await supervisor.recover_orphans()
        await asyncio.wait_for(process.wait(), timeout=10)
        assert "opencode" not in supervisor.runtime.data["hosts"]
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.asyncio
async def test_transient_host_failure_retries_the_worker(runtime, monkeypatch):
    supervisor, host = runtime
    ticket = supervisor.board.create("Task", column="progress")
    state = supervisor.ticket_state(ticket.id)
    state["phase"] = "progress"
    state["iteration"] = 1
    state["developer"]["harness"] = "opencode"
    supervisor.runtime.data["execution"] = "running"
    calls = 0

    async def flaky_host():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("host temporarily unavailable")
        return host

    monkeypatch.setattr(supervisor, "opencode_host", flaky_host)
    try:
        await supervisor.start_worker(ticket, "developer", state["developer"])
        await supervisor.jobs["001:developer"]
        assert calls >= 2
        assert host.created == 1
        assert state["runtime"]["developer"]["status"] == "completed"
    finally:
        await supervisor.terminals.stop()


@pytest.mark.asyncio
async def test_oversized_prompt_fails_without_retries(runtime, monkeypatch):
    supervisor, host = runtime
    ticket = supervisor.board.create("Task", column="progress")
    state = supervisor.ticket_state(ticket.id)
    state["phase"] = "progress"
    state["iteration"] = 1
    state["developer"]["harness"] = "opencode"
    supervisor.runtime.data["execution"] = "running"

    supervisor.runtime.config["developer_prompt"] = "x" * (64 * 1024 + 1)
    try:
        await supervisor.start_worker(ticket, "developer", state["developer"])
        await supervisor.jobs["001:developer"]
        runtime_entry = state["runtime"]["developer"]
        assert host.created == 0
        assert runtime_entry["status"] == "failed"
        assert runtime_entry["attempts"] == 1
        assert "too large" in runtime_entry["error"]
    finally:
        await supervisor.terminals.stop()


@pytest.mark.asyncio
async def test_terminal_attach_recovers_stale_recovery_blocked_entry(runtime):
    supervisor, host = runtime
    host.sessions["ses_test"] = OpenCodeSessionMetadata("ses_test", None, None)
    ticket = supervisor.board.create("Task", column="progress")
    state = supervisor.ticket_state(ticket.id)
    state["developer"] = {"harness": "opencode", "session": "ses_test"}
    state["runtime"]["developer"] = {"status": "running", "harness": "opencode"}
    attached = await supervisor.dispatch({"op": "terminal_attach", "ticket": ticket.id})
    entry = supervisor.terminals.entries[attached["terminal"]]
    try:
        await supervisor.terminals.stop()
        supervisor.terminals.sessions.pop(attached["terminal"])
        entry["status"] = "recovery_blocked"
        entry.pop("pid", None)
        entry.pop("process_identity", None)
        again = await supervisor.dispatch(
            {"op": "terminal_attach", "ticket": ticket.id}
        )
        assert again["terminal"] == attached["terminal"]
        assert entry["status"] == "running"
    finally:
        await supervisor.terminals.stop()


@pytest.mark.asyncio
async def test_resumed_opencode_session_keeps_its_model(runtime):
    supervisor, host = runtime
    ticket = supervisor.board.create("Task", column="progress")
    state = supervisor.ticket_state(ticket.id)
    state["phase"] = "progress"
    state["iteration"] = 1
    state["developer"] = {"harness": "opencode", "session": "ses_test"}
    state["runtime"]["developer"] = {
        "stage": "progress",
        "iteration": 1,
        "status": "running",
    }
    host.sessions["ses_test"] = OpenCodeSessionMetadata("ses_test", None, None)
    supervisor.terminals.entries["cfg"] = {
        "id": "cfg",
        "owner": "custom",
        "role": "config",
        "harness": "opencode",
        "native_session": "ses_config",
    }
    host.sessions["ses_config"] = OpenCodeSessionMetadata(
        "ses_config", None, OpenCodeModel("p", "m", "high")
    )
    supervisor.runtime.data["execution"] = "running"
    try:
        await supervisor.start_worker(ticket, "developer", state["developer"])
        await supervisor.jobs["001:developer"]
        assert host.models == []
        assert host.worker_models[0] is None
    finally:
        await supervisor.terminals.stop()


@pytest.mark.asyncio
async def test_terminal_attach_reports_missing_and_stale_sessions(runtime):
    supervisor, _host = runtime
    ticket = supervisor.board.create("Task", column="progress")
    state = supervisor.ticket_state(ticket.id)
    state["developer"]["harness"] = "opencode"
    state["runtime"]["developer"] = {"status": "running", "harness": "opencode"}
    with pytest.raises(ValueError, match="has not been reported"):
        await supervisor.dispatch({"op": "terminal_attach", "ticket": ticket.id})
    state["developer"]["session"] = "ses_old"
    with pytest.raises(ValueError, match="no longer available"):
        await supervisor.dispatch({"op": "terminal_attach", "ticket": ticket.id})
    assert "session" not in state["developer"]


@pytest.mark.asyncio
async def test_terminal_attach_converts_host_errors(runtime, monkeypatch):
    supervisor, host = runtime
    ticket = supervisor.board.create("Task", column="progress")
    state = supervisor.ticket_state(ticket.id)
    state["developer"] = {"harness": "opencode", "session": "ses_test"}
    state["runtime"]["developer"] = {"status": "running", "harness": "opencode"}

    async def broken():
        raise OSError("host unavailable")

    monkeypatch.setattr(supervisor, "opencode_host", broken)
    with pytest.raises(ValueError, match="host unavailable"):
        await supervisor.dispatch({"op": "terminal_attach", "ticket": ticket.id})

    async def failing_read(session_id):
        raise OpenCodeHttpError(500)

    host.read_session = failing_read

    async def working():
        return host

    monkeypatch.setattr(supervisor, "opencode_host", working)
    with pytest.raises(ValueError, match="HTTP 500"):
        await supervisor.dispatch({"op": "terminal_attach", "ticket": ticket.id})


@pytest.mark.asyncio
async def test_opencode_config_session_uses_the_shared_host(runtime):
    supervisor, host = runtime
    first = await supervisor.dispatch(
        {"op": "session_config_open", "harness": "opencode"}
    )
    entry = supervisor.terminals.entries[first["terminal"]]
    assert entry["role"] == "config"
    assert entry["native_session"] == "ses_test"
    assert entry["status"] == "running"
    again = await supervisor.dispatch(
        {"op": "session_config_open", "harness": "opencode"}
    )
    assert again == first
    assert len(host.sessions) == 1
    await supervisor.terminals.stop()


@pytest.mark.asyncio
async def test_config_open_does_not_start_host_for_running_terminal(
    runtime, monkeypatch
):
    supervisor, _host = runtime
    entry = {
        "id": "cfg",
        "owner": "custom",
        "role": "config",
        "harness": "opencode",
        "status": "running",
        "native_session": "ses_test",
    }
    supervisor.terminals.entries["cfg"] = entry
    supervisor.terminals.sessions["cfg"] = object()

    async def broken():
        raise OSError("host must not start")

    monkeypatch.setattr(supervisor, "opencode_host", broken)
    result = await supervisor.dispatch(
        {"op": "session_config_open", "harness": "opencode"}
    )
    assert result == {"terminal": "cfg"}


@pytest.mark.asyncio
async def test_config_model_failure_does_not_block_worker(runtime):
    supervisor, host = runtime
    ticket = supervisor.board.create("Task", column="progress")
    state = supervisor.ticket_state(ticket.id)
    state["phase"] = "progress"
    state["iteration"] = 1
    state["developer"]["harness"] = "opencode"
    supervisor.terminals.entries["cfg"] = {
        "id": "cfg",
        "owner": "custom",
        "role": "config",
        "harness": "opencode",
        "native_session": "ses_config",
    }

    async def failing_read(session_id):
        raise OpenCodeHostError("config unavailable")

    host.read_session = failing_read
    supervisor.runtime.data["execution"] = "running"
    try:
        await supervisor.start_worker(ticket, "developer", state["developer"])
        await supervisor.jobs["001:developer"]
        assert host.models[0] is None
        assert state["runtime"]["developer"]["status"] == "completed"
    finally:
        await supervisor.terminals.stop()


@pytest.mark.asyncio
async def test_config_session_create_failure_is_value_error(runtime):
    supervisor, host = runtime

    async def failing_create(*, directory=None, model=None):
        raise OpenCodeHostError("create failed")

    host.create_session = failing_create
    with pytest.raises(ValueError, match="Could not create OpenCode config session"):
        await supervisor.dispatch({"op": "session_config_open", "harness": "opencode"})


@pytest.mark.asyncio
async def test_terminal_attach_rejects_unsupported_harness(runtime):
    supervisor, _host = runtime
    ticket = supervisor.board.create("Task", column="progress")
    state = supervisor.ticket_state(ticket.id)
    state["developer"]["harness"] = "pi"
    state["developer"]["session"] = "native-1"
    state["runtime"]["developer"] = {"status": "running", "harness": "pi"}
    with pytest.raises(
        ValueError, match="Live attach is available for OpenCode, Codex and Claude"
    ):
        await supervisor.dispatch({"op": "terminal_attach", "ticket": ticket.id})

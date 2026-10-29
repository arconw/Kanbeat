import asyncio
import os
import subprocess

import pytest

from kanbeat.host_process import HostError, RuntimeIdentity, stop_recorded
from kanbeat.native import process_identity


def alive(pid):
    try:
        state = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "stat="],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return False
    return bool(state) and not state.startswith("Z")


def spawn(new_session):
    return subprocess.Popen(
        ["sleep", "60"], start_new_session=new_session, stdout=subprocess.DEVNULL
    )


@pytest.mark.asyncio
async def test_stop_recorded_kills_matching_group_leader():
    proc = spawn(True)
    runtime = RuntimeIdentity("x", proc.pid, process_identity(proc.pid), "e")
    waiter = asyncio.create_task(asyncio.to_thread(proc.wait))
    assert await stop_recorded(runtime, grace=2) is True
    await waiter
    assert not alive(proc.pid)


@pytest.mark.asyncio
async def test_stop_recorded_refuses_reused_pid_and_non_leader():
    proc = spawn(True)
    try:
        wrong = RuntimeIdentity("x", proc.pid, "linux:1", "e")
        assert await stop_recorded(wrong) is False
        assert alive(proc.pid)
    finally:
        proc.kill()
        proc.wait()
    shared = spawn(False)
    try:
        runtime = RuntimeIdentity("x", shared.pid, process_identity(shared.pid), "e")
        assert os.getpgid(shared.pid) != shared.pid
        assert await stop_recorded(runtime) is False
        assert alive(shared.pid)
    finally:
        shared.kill()
        shared.wait()


@pytest.mark.asyncio
async def test_stop_recorded_ignores_missing_process():
    proc = spawn(True)
    runtime = RuntimeIdentity("x", proc.pid, process_identity(proc.pid), "e")
    proc.kill()
    proc.wait()
    assert await stop_recorded(runtime) is False


def test_runtime_identity_round_trip_and_validation():
    runtime = RuntimeIdentity("codex", 123, "linux:9", "unix:///s")
    assert RuntimeIdentity.from_dict(runtime.to_dict()) == runtime
    for bad in [
        None,
        {},
        {**runtime.to_dict(), "pid": 1},
        {**runtime.to_dict(), "pid": True},
        {**runtime.to_dict(), "identity": ""},
    ]:
        with pytest.raises(HostError):
            RuntimeIdentity.from_dict(bad)

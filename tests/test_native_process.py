import asyncio
import os
import pty
import signal
import sys

import pytest

from kanbeat.launcher import pty_child_command
from kanbeat.native import NativeRun, process_identity, terminate_process


def run_script(tmp_path, monkeypatch, script, prompt="", callback=None):
    monkeypatch.setattr(
        "kanbeat.native.command",
        lambda *args: (
            [sys.executable, "-c", script],
            prompt.encode() if prompt else None,
        ),
    )
    return NativeRun(
        "codex", prompt, None, tmp_path, tmp_path / "native.log", on_process=callback
    )


@pytest.mark.asyncio
async def test_oversized_event_reaps_process_and_readers(tmp_path, monkeypatch):
    before = set(asyncio.all_tasks())
    run = run_script(
        tmp_path,
        monkeypatch,
        "import sys,time; sys.stdout.write('x' * (3 * 1024 * 1024) + '\\n'); sys.stdout.flush(); time.sleep(60)",
    )
    outcome = await asyncio.wait_for(run.execute(), timeout=10)
    assert not outcome.ok
    assert "transport error" in outcome.reason
    assert run.process.returncode is not None
    assert process_identity(run.process.pid) is None
    assert not run.stopping
    assert not set(asyncio.all_tasks()) - before


@pytest.mark.asyncio
async def test_broken_stdin_reaps_process_and_readers(tmp_path, monkeypatch):
    before = set(asyncio.all_tasks())
    run = run_script(
        tmp_path,
        monkeypatch,
        "import os,time; os.close(0); print('READY', flush=True); time.sleep(60)",
        prompt="x" * (8 * 1024 * 1024),
    )
    create_subprocess = asyncio.create_subprocess_exec

    async def start_ready_process(*args, **kwargs):
        process = await create_subprocess(*args, **kwargs)
        try:
            assert (
                await asyncio.wait_for(process.stdout.readline(), timeout=5)
                == b"READY\n"
            )
        except BaseException:
            process.kill()
            await process.wait()
            raise
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start_ready_process)
    outcome = await asyncio.wait_for(run.execute(), timeout=10)
    assert not outcome.ok
    assert any(
        error in outcome.reason for error in ("BrokenPipeError", "ConnectionResetError")
    )
    assert run.process.returncode is not None
    assert process_identity(run.process.pid) is None
    assert not run.stopping
    assert not set(asyncio.all_tasks()) - before


@pytest.mark.asyncio
async def test_process_callback_failure_reaps_process(tmp_path, monkeypatch):
    def fail_callback(*args):
        raise RuntimeError("Callback failed")

    run = run_script(
        tmp_path, monkeypatch, "import time; time.sleep(60)", callback=fail_callback
    )
    with pytest.raises(RuntimeError, match="Callback failed"):
        await asyncio.wait_for(run.execute(), timeout=10)
    assert run.process.returncode is not None
    assert process_identity(run.process.pid) is None


@pytest.mark.asyncio
async def test_cancelled_execution_reaps_process_and_readers(tmp_path, monkeypatch):
    before = set(asyncio.all_tasks())
    started = asyncio.Event()
    run = run_script(
        tmp_path,
        monkeypatch,
        "import time; time.sleep(60)",
        callback=lambda *args: started.set(),
    )
    job = asyncio.create_task(run.execute())
    await asyncio.wait_for(started.wait(), timeout=5)
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(job, timeout=10)
    assert run.process.returncode is not None
    assert process_identity(run.process.pid) is None
    assert not set(asyncio.all_tasks()) - before


@pytest.mark.asyncio
async def test_denied_group_signal_falls_back_to_owned_child(monkeypatch):
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(30)", start_new_session=True
    )
    calls = []
    real_killpg = os.killpg

    def deny_own_group(group, signum):
        if group == proc.pid:
            calls.append(signum)
            raise PermissionError("simulated group denial after hangup")
        return real_killpg(group, signum)

    monkeypatch.setattr("kanbeat.native.os.killpg", deny_own_group)
    try:
        await asyncio.wait_for(terminate_process(proc), timeout=8)
        assert proc.returncode is not None
        assert signal.SIGINT in calls
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


@pytest.mark.asyncio
async def test_changed_process_group_does_not_signal_unverified_group(monkeypatch):
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(30)", start_new_session=True
    )
    groups = []
    monkeypatch.setattr("kanbeat.native.os.getpgid", lambda _pid: -1)
    monkeypatch.setattr("kanbeat.native.os.killpg", lambda *args: groups.append(args))
    try:
        await asyncio.wait_for(terminate_process(proc), timeout=8)
        assert proc.returncode is not None
        assert not groups
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


@pytest.mark.asyncio
async def test_real_pty_hangup_reaps_owned_child():
    master, slave = pty.openpty()
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *pty_child_command(
                [
                    sys.executable,
                    "-u",
                    "-c",
                    "import time; print('READY', flush=True); time.sleep(30)",
                ]
            ),
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
        )
        os.close(slave)
        slave = -1
        loop = asyncio.get_running_loop()
        readable = asyncio.Event()
        loop.add_reader(master, readable.set)
        try:
            await asyncio.wait_for(readable.wait(), timeout=8)
            assert b"READY" in os.read(master, 4096)
        finally:
            loop.remove_reader(master)
        os.close(master)
        master = -1
        await asyncio.wait_for(terminate_process(proc), timeout=8)
        assert proc.returncode is not None
        assert process_identity(proc.pid) is None
    finally:
        if master >= 0:
            os.close(master)
        if slave >= 0:
            os.close(slave)
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.wait()

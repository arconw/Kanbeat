import asyncio
import sys

import pytest

from kanbeat.native import NativeRun, process_identity


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

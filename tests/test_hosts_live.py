import asyncio
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import pytest

from kanbeat.codex_host import CodexAppServerHost, CodexRpcClient
from kanbeat.opencode_host import OpenCodeModel, OpenCodeServeHost

pytestmark = pytest.mark.skipif(
    os.environ.get("KANBEAT_LIVE_HOSTS") != "1",
    reason="needs installed codex/opencode; makes no model calls",
)

DEADLINE = 60
NON_ROUTABLE_PROVIDER = """model = "kb-probe-model"
model_provider = "kbprobe"
[model_providers.kbprobe]
name = "kbprobe"
base_url = "http://127.0.0.1:9/v1"
wire_api = "responses"
requires_openai_auth = false
"""


@pytest.fixture
def scratch():
    root = Path(__file__).resolve().parents[1] / ".tmp"
    root.mkdir(exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="kbl", dir=root))
    for name in ("home", "codex", "cfg", "data", "state", "cache", "cwd", "tmp"):
        (directory / name).mkdir()
    (directory / "codex" / "config.toml").write_text(NON_ROUTABLE_PROVIDER)
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


def scratch_env(scratch: Path) -> dict[str, str]:
    return {
        "PATH": os.environ["PATH"],
        "HOME": str(scratch / "home"),
        "CODEX_HOME": str(scratch / "codex"),
        "TMPDIR": str(scratch / "tmp"),
        "XDG_CONFIG_HOME": str(scratch / "cfg"),
        "XDG_DATA_HOME": str(scratch / "data"),
        "XDG_STATE_HOME": str(scratch / "state"),
        "XDG_CACHE_HOME": str(scratch / "cache"),
    }


def daemon_dir_listing() -> list[str]:
    directory = Path(f"/tmp/codex-daemon-{os.getuid()}")
    return sorted(p.name for p in directory.iterdir()) if directory.is_dir() else []


def alive(pid: int) -> bool:
    return Path(f"/proc/{pid}").exists()


@pytest.mark.asyncio
async def test_two_private_hosts_same_codex_home_are_independent(scratch):
    async def scenario():
        before = daemon_dir_listing()
        env = scratch_env(scratch)
        first = CodexAppServerHost(cwd=scratch / "cwd", env=env)
        second = CodexAppServerHost(cwd=scratch / "cwd", env=env)
        async with first, second:
            assert first.identity.pid != second.identity.pid
            assert first.remote != second.remote
            one = await first.start_thread(scratch / "cwd")
            assert await first.loaded_thread_ids() == [one.thread_id]
            assert await second.loaded_thread_ids() == []
            two = await second.start_thread(scratch / "cwd")
            pid = first.identity.pid
            await first.close()
            assert not alive(pid)
            assert await second.loaded_thread_ids() == [two.thread_id]
            assert (await second.read_thread(two.thread_id)).thread_id == two.thread_id
        assert daemon_dir_listing() == before
        assert not list((scratch / "tmp").iterdir())

    await asyncio.wait_for(scenario(), DEADLINE)


@pytest.mark.asyncio
async def test_private_host_rejects_missing_and_wrong_token(scratch):
    async def scenario():
        async with CodexAppServerHost(
            cwd=scratch / "cwd", env=scratch_env(scratch)
        ) as host:
            with pytest.raises(Exception):
                await CodexRpcClient.connect_ws(host.remote, "wrong", timeout=5)
            observer = await host.connect_observer()
            await observer.close()

    await asyncio.wait_for(scenario(), DEADLINE)


@pytest.mark.asyncio
async def test_exec_does_not_support_remote(scratch):
    async def scenario():
        async with CodexAppServerHost(
            cwd=scratch / "cwd", env=scratch_env(scratch)
        ) as host:
            env = {**host.child_env(), "CODEX_HOME": str(scratch / "codex")}
            result = await asyncio.to_thread(
                subprocess.run,
                [
                    "codex",
                    "--remote",
                    host.remote,
                    "--remote-auth-token-env",
                    "KANBEAT_CODEX_REMOTE_TOKEN",
                    "exec",
                    "--skip-git-repo-check",
                    "x",
                ],
                env=env,
                cwd=scratch / "cwd",
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=30,
            )
            assert result.returncode != 0
            assert b"only supported for interactive" in result.stderr + result.stdout
            assert await host.loaded_thread_ids() == []

    await asyncio.wait_for(scenario(), DEADLINE)


@pytest.mark.asyncio
async def test_name_set_makes_an_empty_thread_resumable_without_turns(scratch):
    async def scenario():
        async with CodexAppServerHost(
            cwd=scratch / "cwd", env=scratch_env(scratch)
        ) as host:
            thread = await host.start_thread(scratch / "cwd")
            with pytest.raises(Exception, match="no rollout"):
                await host.observe_thread(thread.thread_id)
            await host.request(
                "thread/name/set",
                {"threadId": thread.thread_id, "name": "kanbeat slot"},
            )
            observed = await host.observe_thread(thread.thread_id)
            assert observed.thread_id == thread.thread_id and observed.status == "idle"
            resumed = await host.request(
                "thread/resume", {"threadId": thread.thread_id, "excludeTurns": False}
            )
            assert not resumed["thread"].get("turns")
            await host.stop_observing(thread.thread_id)

    await asyncio.wait_for(scenario(), DEADLINE)


@pytest.mark.asyncio
async def test_unix_mode_target_lands_in_tmp_even_with_tmpdir_set(scratch):
    async def scenario():
        before = daemon_dir_listing()
        sock = scratch / "c.sock"
        async with CodexAppServerHost(
            sock, cwd=scratch / "cwd", env=scratch_env(scratch)
        ) as host:
            assert sock.is_symlink()
            assert os.readlink(sock).startswith(f"/tmp/codex-daemon-{os.getuid()}/")
            assert await host.loaded_thread_ids() == []
        assert not sock.exists() and not sock.is_symlink()
        assert daemon_dir_listing() == before

    await asyncio.wait_for(scenario(), DEADLINE)


@pytest.mark.asyncio
async def test_real_opencode_session_model_without_first_message(scratch):
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(scratch / "home"),
        "TMPDIR": str(scratch / "tmp"),
        "XDG_CONFIG_HOME": str(scratch / "cfg"),
        "XDG_DATA_HOME": str(scratch / "data"),
        "XDG_STATE_HOME": str(scratch / "state"),
        "XDG_CACHE_HOME": str(scratch / "cache"),
    }
    async with OpenCodeServeHost(scratch / "cwd", env=env) as host:
        session = await host.create_session(directory=scratch / "cwd")
        assert session.model is None
        changed = await host.set_session_model(
            session.session_id, OpenCodeModel("someprovider", "some-model", "high")
        )
        assert changed.model == OpenCodeModel("someprovider", "some-model", "high")
        assert await host.session_status(session.session_id) == "idle"
        await host.abort(session.session_id)
        assert host.identity.pid

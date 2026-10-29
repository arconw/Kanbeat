import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap

import pytest

from kanbeat.codex_host import (
    CodexAppServerHost,
    CodexDisconnected,
    CodexHostError,
    CodexRpcClient,
    CodexRpcError,
    CodexTimeout,
    parse_thread_metadata,
    tui_argv,
    validate_thread_id,
    viewer_argv,
)
from kanbeat.native import process_identity

THREAD = "01a18660-9ca4-71c0-ba25-81426f4ca65d"
OTHER = "01a18660-9c94-76f3-84bb-bc18608da025"
BAD = "01a18660-9cb4-74e0-9a25-81426f4ca65d"

FAKE_SERVER = textwrap.dedent(
    """\
    #!{python}
    import asyncio, hashlib, json, os, socket, subprocess, sys
    import websockets
    from websockets.asyncio.server import serve, unix_serve

    LOG = os.environ["FAKE_LOG"]
    MODE = os.environ.get("FAKE_MODE", "ok")
    THREADS = {{
        "{thread}": {{"id": "{thread}", "model": "gpt-x", "modelProvider": "openai",
                     "reasoningEffort": "high", "cwd": "/work",
                     "status": {{"type": "idle"}}, "preview": "SECRET PREVIEW",
                     "path": "/home/u/rollout.jsonl", "turns": [{{"secret": "history"}}]}},
    }}

    def log(entry):
        with open(LOG, "a") as handle:
            handle.write(json.dumps(entry) + "\\n")

    async def handler(ws):
        sub = set()
        async for raw in ws:
            msg = json.loads(raw)
            log(msg)
            method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {{}}
            if mid is None:
                continue
            if method == "initialize":
                result = {{"userAgent": "fake/1", "codexHome": "/x"}}
            elif method == "thread/start":
                tid = "{other}"
                THREADS[tid] = {{"id": tid, "model": "gpt-x", "modelProvider": "openai",
                                "reasoningEffort": None, "cwd": params.get("cwd"),
                                "status": {{"type": "idle"}}}}
                result = {{"thread": THREADS[tid], "model": "gpt-x", "modelProvider": "openai",
                          "reasoningEffort": None, "approvalPolicy": "on-request"}}
                await ws.send(json.dumps({{"method": "thread/started", "params": {{"thread": THREADS[tid]}}}}))
            elif method == "thread/read":
                if params.get("threadId") == "{bad}":
                    await ws.send(json.dumps({{"id": mid, "error": {{"code": -32600, "message": "still creating"}}}}))
                    continue
                thread = THREADS.get(params.get("threadId"))
                if thread is None:
                    await ws.send(json.dumps({{"id": mid, "error": {{"code": -32600, "message": "no thread"}}}}))
                    continue
                result = {{"thread": thread}}
            elif method == "thread/resume":
                result = {{"thread": THREADS[params["threadId"]]}}
            elif method == "thread/unsubscribe":
                result = {{"status": "unsubscribed"}}
            elif method == "thread/loaded/list":
                result = {{"data": list(THREADS) + ["{bad}"], "nextCursor": None}}
            elif method == "test/notify":
                for target in ("{thread}", "{other}"):
                    await ws.send(json.dumps({{"method": "turn/completed", "params": {{"threadId": target, "n": 1}}}}))
                await ws.send(json.dumps({{"method": "item/started", "params": {{"threadId": "{thread}"}}}}))
                result = {{}}
            elif method == "test/serverrequest":
                await ws.send(json.dumps({{"id": 900, "method": "item/commandExecution/requestApproval", "params": {{"threadId": "{thread}"}}}}))
                result = {{}}
            elif method == "test/hang":
                continue
            elif method == "test/die":
                await ws.close()
                return
            else:
                result = {{}}
            await ws.send(json.dumps({{"id": mid, "result": result}}))

    async def main():
        listen = sys.argv[sys.argv.index("--listen") + 1]
        if MODE == "exit":
            sys.exit(3)
        if MODE == "hang":
            await asyncio.sleep(3600)
        child = subprocess.Popen(["sleep", "3600"])
        log({{"child": child.pid, "pid": os.getpid(), "argv": sys.argv[1:], "token_env": "KANBEAT_CODEX_REMOTE_TOKEN" in os.environ}})
        if listen.startswith("ws://"):
            digest = sys.argv[sys.argv.index("--ws-token-sha256") + 1]
            def check(connection, request):
                header = request.headers.get("Authorization", "")
                if hashlib.sha256(header.removeprefix("Bearer ").encode()).hexdigest() != digest:
                    return connection.respond(401, "unauthorized\\n")
            async with serve(handler, "127.0.0.1", 0, process_request=check) as server:
                port = server.sockets[0].getsockname()[1]
                sys.stdout.write("noise " * 20000 + "\\n")
                sys.stdout.write("codex app-server (WebSockets)\\n  listening on: ws://127.0.0.1:%d\\n  readyz: x\\n" % port)
                sys.stdout.flush()
                await asyncio.sleep(3600)
            return
        path = listen[len("unix://"):]
        if MODE == "symlink_exit":
            real = path + ".real"
            stale = socket.socket(socket.AF_UNIX)
            stale.bind(real)
            stale.close()
            os.symlink(real, path)
            sys.exit(4)
        if MODE == "symlink":
            real = path + ".real"
            async with unix_serve(handler, real):
                os.symlink(real, path)
                await asyncio.sleep(3600)
            return
        async with unix_serve(handler, path):
            await asyncio.sleep(3600)

    asyncio.run(main())
    """
)


@pytest.fixture
def short_dir():
    root = Path(__file__).resolve().parents[1] / ".tmp"
    root.mkdir(exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="kbh", dir=root))
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def fake_codex(short_dir):
    path = short_dir / "codex"
    path.write_text(
        FAKE_SERVER.format(python=sys.executable, thread=THREAD, other=OTHER, bad=BAD)
    )
    path.chmod(0o755)
    return path


def make_host(short_dir, fake_codex, mode="ok", socket_path="default", **kwargs):
    env = {
        "PATH": os.environ["PATH"],
        "TMPDIR": str(short_dir),
        "FAKE_LOG": str(short_dir / "log"),
        "FAKE_MODE": mode,
    }
    return CodexAppServerHost(
        short_dir / "c.sock" if socket_path == "default" else socket_path,
        cwd=short_dir,
        env=env,
        executable=str(fake_codex),
        **kwargs,
    )


def logged(short_dir):
    path = short_dir / "log"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


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


def command_line(pid):
    proc = Path(f"/proc/{pid}/cmdline")
    if proc.exists():
        return proc.read_bytes()
    return subprocess.check_output(
        ["ps", "-p", str(pid), "-o", "command="], stderr=subprocess.DEVNULL
    )


def test_viewer_and_tui_argv():
    sock = Path("/home/u/.tmp/s.sock")
    assert viewer_argv(sock, THREAD.upper()) == [
        "codex",
        "--remote",
        "unix:///home/u/.tmp/s.sock",
        "resume",
        THREAD,
    ]
    assert tui_argv(sock) == ["codex", "--remote", "unix:///home/u/.tmp/s.sock"]
    assert tui_argv(sock, thread_id=THREAD, prompt="hello")[-3:] == [
        "resume",
        THREAD,
        "hello",
    ]


@pytest.mark.parametrize("bad", ["--evil", "", "../x", THREAD + "x", None, 7])
def test_thread_id_validation(bad):
    with pytest.raises(CodexHostError):
        validate_thread_id(bad)


def test_argv_rejects_unsafe_inputs():
    with pytest.raises(CodexHostError):
        viewer_argv(Path("/a/s.sock"), "--resume")
    with pytest.raises(CodexHostError):
        viewer_argv(Path("relative.sock"), THREAD)
    with pytest.raises(CodexHostError):
        viewer_argv(Path("/a b/s.sock"), THREAD)
    with pytest.raises(CodexHostError):
        viewer_argv(Path("/" + "a" * 120 + "/s.sock"), THREAD)
    with pytest.raises(CodexHostError):
        tui_argv(Path("/a/s.sock"), prompt="--dangerously-bypass")


def test_parse_thread_metadata_keeps_only_allowed_fields():
    payload = {
        "model": "top-model",
        "thread": {
            "id": THREAD,
            "modelProvider": "openai",
            "reasoningEffort": "bad\nvalue",
            "status": {"type": "active", "activeFlags": ["waitingOnApproval", 5]},
            "preview": "SECRET",
            "path": "/rollout",
            "turns": [{"x": 1}],
        },
    }
    metadata = parse_thread_metadata(payload)
    assert metadata.model == "top-model"
    assert metadata.model_provider == "openai"
    assert metadata.reasoning_effort is None
    assert metadata.status == "active"
    assert metadata.active_flags == ("waitingOnApproval",)
    assert "SECRET" not in repr(metadata.to_dict())
    with pytest.raises(CodexHostError):
        parse_thread_metadata({"thread": {"id": "nope"}})
    with pytest.raises(CodexHostError):
        parse_thread_metadata({})


@pytest.mark.asyncio
async def test_host_lifecycle_metadata_and_cleanup(short_dir, fake_codex):
    host = make_host(short_dir, fake_codex)
    identity = await host.start()
    assert identity.kind == "codex"
    assert identity.endpoint == "unix://" + str(short_dir / "c.sock")
    assert process_identity(identity.pid) == identity.identity
    assert os.getpgid(identity.pid) == identity.pid
    assert stat.S_IMODE((short_dir / "c.sock").stat().st_mode) & 0o077 == 0
    await asyncio.sleep(0.2)
    entries = logged(short_dir)
    assert entries[0]["argv"] == ["app-server", "--listen", host.remote]
    child = entries[0]["child"]
    assert [e.get("method") for e in entries[1:3]] == ["initialize", "initialized"]

    started = await host.start_thread(short_dir)
    assert started.thread_id == OTHER and started.cwd == str(short_dir)
    metadata = await host.read_thread(THREAD)
    assert (metadata.model, metadata.model_provider, metadata.reasoning_effort) == (
        "gpt-x",
        "openai",
        "high",
    )
    assert metadata.status == "idle"
    reads = [e for e in logged(short_dir) if e.get("method") == "thread/read"]
    assert reads and all(e["params"]["includeTurns"] is False for e in reads)
    assert set(await host.loaded_thread_ids()) == {THREAD, OTHER, BAD}
    with pytest.raises(CodexRpcError):
        await host.read_thread("01a18660-0000-7000-8000-000000000000")

    await host.close()
    await host.close()
    assert not (short_dir / "c.sock").exists()
    assert not alive(identity.pid)
    assert not alive(child)
    with pytest.raises(CodexDisconnected):
        await host.request("thread/loaded/list")


@pytest.mark.asyncio
async def test_notifications_are_filtered_by_thread(short_dir, fake_codex):
    async with make_host(short_dir, fake_codex) as host:
        mine = host.subscribe(THREAD)
        everything = host.subscribe()
        only_items = host.subscribe(THREAD, methods=["item/started"])
        await host.request("test/notify")
        first = await mine.get(2)
        second = await mine.get(2)
        assert (first.method, first.thread_id) == ("turn/completed", THREAD)
        assert second.method == "item/started"
        with pytest.raises(CodexTimeout):
            await mine.get(0.2)
        assert [(await everything.get(2)).thread_id for _ in range(3)] == [
            THREAD,
            OTHER,
            THREAD,
        ]
        assert (await only_items.get(2)).method == "item/started"
        mine.close()
        with pytest.raises(CodexDisconnected):
            await mine.get(1)


@pytest.mark.asyncio
async def test_subscription_queue_is_bounded(short_dir, fake_codex):
    async with make_host(short_dir, fake_codex) as host:
        small = host.subscribe(THREAD, maxsize=1)
        await host.request("test/notify")
        await host.request("test/notify")
        await host.request("loaded")
        assert small.dropped >= 1
        assert (await small.get(1)).method == "item/started"


@pytest.mark.asyncio
async def test_request_timeout_and_cancellation_clear_pending(short_dir, fake_codex):
    async with make_host(short_dir, fake_codex) as host:
        with pytest.raises(CodexTimeout):
            await host.request("test/hang", timeout=0.2)
        task = asyncio.create_task(host.request("test/hang", timeout=30))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert host.client._pending == {}
        assert (await host.request("thread/loaded/list"))["data"]


@pytest.mark.asyncio
async def test_disconnect_fails_pending_and_subscriptions(short_dir, fake_codex):
    async with make_host(short_dir, fake_codex) as host:
        subscription = host.subscribe(THREAD)
        hanging = asyncio.create_task(host.request("test/hang", timeout=30))
        await asyncio.sleep(0.1)
        with pytest.raises(CodexDisconnected):
            await host.request("test/die", timeout=5)
        with pytest.raises(CodexDisconnected):
            await hanging
        with pytest.raises(CodexDisconnected):
            await subscription.get(1)
        with pytest.raises(CodexDisconnected):
            await host.request("thread/loaded/list")


@pytest.mark.asyncio
async def test_server_requests_are_rejected_not_answered(short_dir, fake_codex):
    async with make_host(short_dir, fake_codex) as host:
        await host.request("test/serverrequest")
        await asyncio.sleep(0.2)
        replies = [e for e in logged(short_dir) if e.get("id") == 900]
        assert replies and replies[0]["error"]["code"] == -32601
        assert "result" not in replies[0]


@pytest.mark.asyncio
async def test_observe_and_wait_for_new_thread(short_dir, fake_codex):
    async with make_host(short_dir, fake_codex) as host:
        metadata = await host.observe_thread(THREAD)
        assert metadata.thread_id == THREAD
        resume = [e for e in logged(short_dir) if e.get("method") == "thread/resume"][0]
        assert resume["params"] == {"threadId": THREAD, "excludeTurns": True}
        await host.stop_observing(THREAD)
        with pytest.raises(CodexTimeout):
            await host.wait_for_new_thread([THREAD], timeout=0.3, poll_interval=0.1)
        await host.start_thread(short_dir)
        found = await host.wait_for_new_thread(
            [THREAD], cwd=short_dir, timeout=2, poll_interval=0.1
        )
        assert found.thread_id == OTHER
        with pytest.raises(CodexTimeout):
            await host.wait_for_new_thread(
                [THREAD], cwd=Path("/nowhere"), timeout=0.3, poll_interval=0.1
            )


@pytest.mark.asyncio
async def test_wait_for_new_thread_skips_errors_and_normalizes_cwd(
    short_dir, fake_codex
):
    real = short_dir / "real-cwd"
    real.mkdir()
    link = short_dir / "linked-cwd"
    link.symlink_to(real, target_is_directory=True)
    async with make_host(short_dir, fake_codex) as host:
        await host.start_thread(real)
        found = await host.wait_for_new_thread(
            [THREAD], cwd=link, timeout=2, poll_interval=0.05
        )
    assert found.thread_id == OTHER


@pytest.mark.asyncio
async def test_startup_exit_and_timeout_clean_up(short_dir, fake_codex):
    host = make_host(short_dir, fake_codex, mode="exit")
    with pytest.raises(CodexHostError, match="exited during startup"):
        await host.start()
    assert host.identity is None or not alive(host.identity.pid)

    host = make_host(short_dir, fake_codex, mode="hang", startup_timeout=0.5)
    with pytest.raises(CodexTimeout):
        await host.start()
    pid = host.identity.pid
    assert not alive(pid)
    assert not (short_dir / "c.sock").exists()
    with pytest.raises(CodexHostError):
        await host.start()


@pytest.mark.asyncio
async def test_client_connects_to_existing_socket_as_observer(short_dir, fake_codex):
    async with make_host(short_dir, fake_codex) as host:
        observer = await CodexRpcClient.connect(short_dir / "c.sock", client_name="obs")
        try:
            assert observer.user_agent == "fake/1"
            result = await observer.request(
                "thread/read", {"threadId": THREAD, "includeTurns": False}
            )
            assert result["thread"]["id"] == THREAD
        finally:
            await observer.close()
        assert not host.client.closed


@pytest.mark.asyncio
async def test_stale_socket_is_replaced_and_foreign_path_refused(short_dir, fake_codex):
    import socket as socket_module

    stale = socket_module.socket(socket_module.AF_UNIX)
    stale.bind(str(short_dir / "c.sock"))
    stale.close()
    async with make_host(short_dir, fake_codex) as host:
        assert (await host.request("thread/loaded/list"))["data"]
    regular = short_dir / "c.sock"
    regular.write_text("not a socket")
    host = make_host(short_dir, fake_codex)
    with pytest.raises(RuntimeError):
        await host.start()
    assert regular.read_text() == "not a socket"


@pytest.mark.asyncio
async def test_preexisting_symlink_is_refused_and_target_untouched(
    short_dir, fake_codex
):
    import socket as socket_module

    real = short_dir / "real.sock"
    link = short_dir / "c.sock"
    foreign = socket_module.socket(socket_module.AF_UNIX)
    foreign.bind(str(real))
    foreign.listen()
    link.symlink_to(real)
    host = make_host(short_dir, fake_codex)
    with pytest.raises(RuntimeError):
        await host.start()
    assert link.is_symlink() and real.exists()
    foreign.close()


@pytest.mark.asyncio
async def test_live_listener_at_path_is_refused(short_dir, fake_codex):
    import socket as socket_module

    foreign = socket_module.socket(socket_module.AF_UNIX)
    foreign.bind(str(short_dir / "c.sock"))
    foreign.listen()
    host = make_host(short_dir, fake_codex)
    with pytest.raises(RuntimeError):
        await host.start()
    assert (short_dir / "c.sock").exists()
    foreign.close()


@pytest.mark.asyncio
async def test_stale_socket_symlink_and_target_are_replaced(short_dir, fake_codex):
    import socket as socket_module

    real = short_dir / "c.sock.real"
    stale = socket_module.socket(socket_module.AF_UNIX)
    stale.bind(str(real))
    stale.close()
    link = short_dir / "c.sock"
    link.symlink_to(real)
    async with make_host(short_dir, fake_codex, mode="symlink") as host:
        assert (await host.request("thread/loaded/list"))["data"]
    assert not link.is_symlink()
    assert not real.exists()


@pytest.mark.asyncio
async def test_symlink_to_missing_target_is_removed(short_dir, fake_codex):
    link = short_dir / "c.sock"
    link.symlink_to(short_dir / "missing.sock")
    async with make_host(short_dir, fake_codex) as host:
        assert (await host.request("thread/loaded/list"))["data"]
    assert not link.is_symlink()


@pytest.mark.asyncio
async def test_failed_start_cleans_codex_created_link(short_dir, fake_codex):
    host = make_host(short_dir, fake_codex, mode="symlink_exit")
    with pytest.raises(CodexHostError):
        await host.start()
    link = short_dir / "c.sock"
    assert not link.is_symlink()
    assert not (short_dir / "c.sock.real").exists()


@pytest.mark.asyncio
async def test_codex_created_symlink_and_target_are_cleaned(short_dir, fake_codex):
    host = make_host(short_dir, fake_codex, mode="symlink")
    await host.start()
    link = short_dir / "c.sock"
    assert link.is_symlink() and (short_dir / "c.sock.real").exists()
    await host.close()
    assert not link.is_symlink() and not (short_dir / "c.sock.real").exists()


@pytest.mark.asyncio
async def test_swapped_link_target_is_not_removed_on_close(short_dir, fake_codex):
    import socket as socket_module

    host = make_host(short_dir, fake_codex, mode="symlink")
    await host.start()
    link = short_dir / "c.sock"
    other = short_dir / "other.sock"
    foreign = socket_module.socket(socket_module.AF_UNIX)
    foreign.bind(str(other))
    link.unlink()
    link.symlink_to(other)
    await host.close()
    assert other.exists()
    foreign.close()


def private_host(short_dir, fake_codex, mode="ok", **kwargs):
    return make_host(short_dir, fake_codex, mode=mode, socket_path=None, **kwargs)


@pytest.mark.asyncio
async def test_private_mode_uses_ephemeral_loopback_port_and_token_only_in_env(
    short_dir, fake_codex
):
    host = private_host(short_dir, fake_codex)
    async with host:
        identity = host.identity
        assert identity.endpoint.startswith("ws://127.0.0.1:")
        assert host.remote == identity.endpoint
        token = host.child_env()["KANBEAT_CODEX_REMOTE_TOKEN"]
        entry = [e for e in logged(short_dir) if "argv" in e][0]
        assert entry["token_env"] is False
        assert "--ws-auth" in entry["argv"] and "capability-token" in entry["argv"]
        assert hashlib.sha256(token.encode()).hexdigest() in entry["argv"]
        everything = (
            json.dumps([entry, identity.to_dict()]) + (short_dir / "log").read_text()
        )
        assert token not in everything
        cmdline = command_line(identity.pid)
        assert token.encode() not in cmdline
        assert (await host.request("thread/loaded/list"))["data"]
        observer = await host.connect_observer()
        try:
            assert (await observer.request("thread/loaded/list"))["data"]
        finally:
            await observer.close()
        assert host.viewer_argv(THREAD) == [
            str(fake_codex),
            "--remote",
            identity.endpoint,
            "--remote-auth-token-env",
            "KANBEAT_CODEX_REMOTE_TOKEN",
            "resume",
            THREAD,
        ]
        assert token not in " ".join(host.tui_argv(prompt="hi"))
        pid = identity.pid
    assert (
        not Path(f"/proc/{pid}").exists() or process_identity(pid) != identity.identity
    )


@pytest.mark.asyncio
async def test_private_mode_rejects_wrong_token_and_survives_noisy_output(
    short_dir, fake_codex
):
    async with private_host(short_dir, fake_codex) as host:
        with pytest.raises(CodexDisconnected):
            await CodexRpcClient.connect_ws(host.remote, "wrong", timeout=3)
        assert (await host.request("thread/loaded/list"))["data"]


@pytest.mark.asyncio
async def test_two_private_hosts_are_independent(short_dir, fake_codex):
    first, second = (
        private_host(short_dir, fake_codex),
        private_host(short_dir, fake_codex),
    )
    await first.start()
    await second.start()
    assert first.identity.pid != second.identity.pid
    assert first.remote != second.remote
    assert (
        first.child_env()["KANBEAT_CODEX_REMOTE_TOKEN"]
        != second.child_env()["KANBEAT_CODEX_REMOTE_TOKEN"]
    )
    await first.close()
    assert (await second.request("thread/loaded/list"))["data"]
    await second.close()


@pytest.mark.asyncio
async def test_private_mode_startup_failures_clean_up(short_dir, fake_codex):
    for mode in ("exit", "hang"):
        host = private_host(short_dir, fake_codex, mode=mode, startup_timeout=1.0)
        with pytest.raises(CodexHostError):
            await host.start()
        assert host.identity is None or process_identity(host.identity.pid) is None


def test_ws_remote_must_be_loopback_with_port():
    for bad in (
        "ws://example.com:80",
        "ws://127.0.0.1",
        "ws://127.0.0.1:0",
        "ws://127.0.0.1:99999",
    ):
        with pytest.raises(CodexHostError):
            tui_argv(bad)

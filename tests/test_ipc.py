import asyncio

import pytest

from kanbeat.supervisor import Supervisor, request


@pytest.mark.asyncio
async def test_ipc(tmp_path):
    supervisor = Supervisor(tmp_path)
    server = await asyncio.start_unix_server(
        supervisor.handle, path=str(supervisor.socket)
    )
    try:
        await request(
            tmp_path,
            {
                "op": "save_config",
                "values": {
                    "transition_hook": {"harness": "claude", "prompt": "Sync issue"},
                },
            },
        )
        created = await request(tmp_path, {"op": "create", "title": "IPC task"})
        ticket = created["tickets"][0]
        moved = await request(
            tmp_path, {"op": "move", "ticket": ticket["id"], "column": "todo"}
        )
        assert moved["tickets"][0]["column"] == "todo"
        assert "hooks" not in moved and "event_seq" in moved
        with pytest.raises(RuntimeError, match="Invalid transition hook harness"):
            await request(
                tmp_path,
                {
                    "op": "save_config",
                    "values": {
                        "transition_hook": {"harness": "invalid"},
                    },
                },
            )
        assert (await request(tmp_path, {"op": "snapshot"}))["execution"] == "stopped"
    finally:
        server.close()
        await server.wait_closed()
        supervisor.socket.unlink(missing_ok=True)


@pytest.fixture
def short_socket_root(tmp_path_factory, monkeypatch):
    root = tmp_path_factory.getbasetemp().parent / "sockets"
    monkeypatch.setattr("kanbeat.ipc.socket_root", lambda: root)
    return root


@pytest.mark.asyncio
async def test_long_paths(tmp_path, short_socket_root):
    import os

    from kanbeat.ipc import socket_path

    projects = [tmp_path / ("nested-" * 15) / name for name in ("one", "two")]
    supervisors = [Supervisor(project) for project in projects]
    servers = []
    try:
        for supervisor in supervisors:
            supervisor.acquire()
            servers.append(
                await asyncio.start_unix_server(
                    supervisor.handle, path=str(supervisor.socket)
                )
            )
            os.chmod(supervisor.socket, 0o600)
        assert supervisors[0].socket != supervisors[1].socket
        assert all(len(os.fsencode(s.socket)) <= 103 for s in supervisors)
        assert short_socket_root.stat().st_mode & 0o777 == 0o700
        assert supervisors[0].socket.stat().st_mode & 0o777 == 0o600
        await request(projects[0], {"op": "create", "title": "Only first"})
        assert len((await request(projects[0], {"op": "snapshot"}))["tickets"]) == 1
        assert not (await request(projects[1], {"op": "snapshot"}))["tickets"]
        alias = tmp_path / "alias"
        alias.symlink_to(projects[0], target_is_directory=True)
        assert socket_path(alias) == supervisors[0].socket
        assert len((await request(alias, {"op": "snapshot"}))["tickets"]) == 1
        with pytest.raises(RuntimeError, match="already owns"):
            Supervisor(projects[0]).acquire()
    finally:
        for server in servers:
            server.close()
            await server.wait_closed()
        for supervisor in supervisors:
            supervisor.socket.unlink(missing_ok=True)
            if supervisor.lock_stream:
                supervisor.lock_stream.close()


def test_socket_cleanup_refuses_regular_files_and_symlinks(tmp_path):
    from kanbeat.ipc import remove_socket

    path = tmp_path / "socket"
    path.write_text("Keep me")
    with pytest.raises(RuntimeError, match="Refusing"):
        remove_socket(path)
    alias = tmp_path / "alias"
    alias.symlink_to(path)
    with pytest.raises(RuntimeError, match="Refusing"):
        remove_socket(alias)
    assert path.read_text() == "Keep me"


def test_stale_socket_removed_only_by_lock_owner(tmp_path, short_socket_root):
    import socket

    from kanbeat.ipc import prepare_socket_directory

    project = tmp_path / ("nested-" * 15)
    supervisor = Supervisor(project)
    prepare_socket_directory(supervisor.socket)
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(supervisor.socket))
    stale.close()
    try:
        supervisor.acquire()
        assert not supervisor.socket.exists()
    finally:
        supervisor.lock_stream.close()


def test_socket_directory_rejects_symlink(tmp_path):
    from kanbeat.ipc import prepare_socket_directory

    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(RuntimeError, match="real directory"):
        prepare_socket_directory(alias / "test.sock")


def test_shared_project_parent_is_allowed(tmp_path):
    from kanbeat.ipc import prepare_socket_directory

    state = tmp_path / ".kanbeat" / ".state"
    state.mkdir(parents=True)
    state.parent.chmod(0o770)
    prepare_socket_directory(state / "supervisor.sock")
    assert state.stat().st_mode & 0o777 == 0o700


def test_bind_permissions_are_private_immediately(tmp_path):
    import os

    from kanbeat.ipc import bind_socket

    path = tmp_path / "test.sock"
    previous = os.umask(0)
    sock = None
    try:
        sock = bind_socket(path)
        assert path.stat().st_mode & 0o777 == 0o600
        assert os.umask(0) == 0
    finally:
        os.umask(previous)
        if sock is not None:
            sock.close()
        path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_shutdown_preserves_replacement_file_and_releases_lock(
    tmp_path, short_socket_root, caplog
):
    supervisor = Supervisor(tmp_path / ("nested-" * 15))
    task = asyncio.create_task(supervisor.serve())
    try:
        for _ in range(100):
            if supervisor.server is not None:
                break
            await asyncio.sleep(0.01)
        assert supervisor.server is not None
        supervisor.socket.unlink()
        supervisor.socket.write_text("Preserve replacement")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert supervisor.socket.read_text() == "Preserve replacement"
        assert "Socket cleanup" in caplog.text
        assert supervisor.lock_stream is None
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        supervisor.socket.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_shutdown_requires_confirmation_for_independent_sessions(
    tmp_path, short_socket_root, monkeypatch
):
    import sys

    from kanbeat.native import process_identity

    monkeypatch.setattr(
        "kanbeat.terminals.interactive_command",
        lambda *args: [sys.executable, "-c", "import time; time.sleep(60)"],
    )
    supervisor = Supervisor(tmp_path)
    task = asyncio.create_task(supervisor.serve())
    try:
        for _ in range(100):
            if supervisor.server is not None:
                break
            await asyncio.sleep(0.01)
        terminal_id = await supervisor.terminals.create("codex", owner="custom")
        pid = supervisor.terminals.get(terminal_id).process.pid
        with pytest.raises(RuntimeError, match="shutdown --yes"):
            await request(tmp_path, {"op": "shutdown"})
        assert not supervisor.shutdown_requested.is_set()
        assert (await request(tmp_path, {"op": "shutdown", "confirmed": True}))[
            "shutdown"
        ] == "requested"
        await asyncio.wait_for(task, timeout=10)
        assert process_identity(pid) is None
        assert not supervisor.socket.exists()
        assert supervisor.lock_stream is None
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await supervisor.terminals.stop()


@pytest.mark.asyncio
async def test_queued_mutation_is_rejected_after_shutdown(tmp_path):
    supervisor = Supervisor(tmp_path)
    async with supervisor.guard:
        stopping = asyncio.create_task(supervisor.dispatch({"op": "shutdown"}))
        creating = asyncio.create_task(
            supervisor.dispatch({"op": "group_create", "name": "Too late"})
        )
        await asyncio.sleep(0)
    assert await stopping == {"shutdown": "requested"}
    with pytest.raises(RuntimeError, match="shutting down"):
        await creating
    assert not supervisor.session_groups.groups

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
        assert moved["hooks"]["1"]["status"] == "pending"
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

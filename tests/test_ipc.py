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

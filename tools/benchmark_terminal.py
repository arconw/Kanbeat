from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import statistics
import sys
import time
from unittest.mock import patch

from textual import events

from kanbeat.ipc import bind_socket, remove_socket
from kanbeat.supervisor import Supervisor
from kanbeat.ui.app import KanbeatApp
from kanbeat.ui.terminal import TerminalDisplay, TerminalView

FIXTURE = """
import os, time, tty
tty.setraw(0)
os.write(1, b'READY\\r\\n')
while True:
    data = os.read(0, 4096)
    if not data:
        break
    time.sleep(DELAY)
    os.write(1, data)
"""


async def measure(work: Path, delay: float, count: int):
    work.mkdir(parents=True, exist_ok=False)
    socket = work / "supervisor.sock"
    fixture = FIXTURE.replace("DELAY", repr(delay))
    with (
        patch("kanbeat.supervisor.socket_path", lambda project: socket),
        patch(
            "kanbeat.terminals.interactive_command",
            lambda *args: [sys.executable, "-u", "-c", fixture],
        ),
        patch("kanbeat.supervisor.available", lambda name: True),
    ):
        supervisor = Supervisor(work / "project")
        server = await asyncio.start_unix_server(
            supervisor.handle, sock=bind_socket(socket)
        )
        app = KanbeatApp(supervisor.board.project)
        ticket = supervisor.board.create("Fixture benchmark")
        terminal_id = (
            await supervisor.dispatch({"op": "terminal_open", "ticket": ticket.id})
        )["terminal"]
        try:
            async with app.run_test(size=(100, 40)) as pilot:
                app.push_screen(TerminalView(terminal_id))
                await pilot.pause()
                display = app.screen.query_one(TerminalDisplay)

                async def visible(text: str, amount: int, timeout=5):
                    deadline = time.perf_counter() + timeout
                    while display.board.capture_text().count(text) < amount:
                        if time.perf_counter() >= deadline:
                            raise RuntimeError(
                                "Fixture output did not reach the display"
                            )
                        await asyncio.sleep(0.0005)

                await visible("READY", 1)
                latency = []
                for index in range(count):
                    character = chr(97 + index % 26)
                    before = display.board.capture_text().count(character)
                    started = time.perf_counter()
                    display.post_message(events.Key(character, character))
                    await visible(character, before + 1)
                    latency.append((time.perf_counter() - started) * 1000)
                    await asyncio.sleep(0.03)
                before = display.board.capture_text().count("z")
                started = time.perf_counter()
                for _ in range(40):
                    display.post_message(events.Key("z", "z"))
                await visible("z", before + 40)
                burst = (time.perf_counter() - started) * 1000
                samples = sorted(latency)
                p95 = samples[min(len(samples) - 1, int(len(samples) * 0.95))]
                print(
                    f"delay={delay * 1000:g}ms samples={count} median={statistics.median(latency):.1f}ms p95={p95:.1f}ms burst40={burst:.1f}ms"
                )
        finally:
            await supervisor.terminals.stop()
            server.close()
            await server.wait_closed()
            remove_socket(socket)


def main():
    parser = argparse.ArgumentParser(
        description="Measure real PTY input-to-Textual-screen latency with a deterministic echo fixture; no model calls."
    )
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--delay", type=float, default=0.03)
    parser.add_argument("--count", type=int, default=60)
    args = parser.parse_args()
    if args.delay < 0 or not 1 <= args.count <= 1000:
        parser.error("delay must be nonnegative and count must be 1..1000")
    asyncio.run(measure(args.work.resolve(), args.delay, args.count))


if __name__ == "__main__":
    main()

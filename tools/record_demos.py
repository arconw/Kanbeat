from __future__ import annotations

import argparse
import asyncio
import getpass
from contextlib import asynccontextmanager
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

from demo_frames import PATH_PLACEHOLDER, FrameRenderer, font_file, save_gif

TERMINAL_SIZE = (128, 30)
MARKER = ".kanbeat-demo-workdir"
HARNESS_NAMES = ("codex", "claude", "opencode", "pi")
STAND_IN = """#!/bin/sh
printf '\\033[1;36mKanbeat demo stand-in\\033[0m\\n'
printf 'A plain shell script named "%s". No model is attached.\\n' "$(basename "$0")"
printf 'Type a line and press Enter to see the PTY round trip.\\n\\n'
while printf '> ' && IFS= read -r line; do
    printf 'received: %s\\n' "$line"
done
"""


class Demo:
    def __init__(self, workdir: Path):
        self.workdir = workdir.resolve()
        self.bin = self.workdir / "bin"
        self.processes: dict[Path, subprocess.Popen] = {}
        self.started: list[subprocess.Popen] = []
        self.env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": os.environ["HOME"],
            "LANG": "C.UTF-8",
            "TERM": "xterm-256color",
            "TMPDIR": str(self.workdir),
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    def prepare(self) -> None:
        marker = self.workdir / MARKER
        if self.workdir.exists() and not marker.exists():
            raise SystemExit(
                f"Refusing to reuse a directory I did not create: {self.workdir}"
            )
        self.workdir.mkdir(parents=True, exist_ok=True)
        marker.touch()
        self.bin.mkdir(exist_ok=True)
        for name in HARNESS_NAMES:
            script = self.bin / name
            script.write_text(STAND_IN, encoding="utf-8")
            script.chmod(0o755)

    def project(self, name: str) -> Path:
        path = self.workdir / name
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True)
        return path

    def cli(self, project: Path, *arguments: str) -> None:
        subprocess.run(
            [sys.executable, "-m", "kanbeat", "--project", str(project), *arguments],
            env=self.env,
            cwd=project,
            check=True,
            stdin=subprocess.DEVNULL,
        )

    def tickets(self, project: Path) -> dict[str, list[str]]:
        board = project / ".kanbeat"
        return {
            column: sorted(path.name for path in (board / column).glob("*.md"))
            for column in ("backlog", "todo", "progress", "review", "done", "failed")
        }

    @asynccontextmanager
    async def supervisor(self, project: Path):
        from kanbeat.supervisor import request

        log = (self.workdir / f"{project.name}-supervisor.log").open(
            "w", encoding="utf-8"
        )
        process = subprocess.Popen(
            [sys.executable, "-m", "kanbeat", "--project", str(project), "supervisor"],
            env=self.env,
            cwd=project,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        self.processes[project] = process
        self.started.append(process)
        try:
            for _ in range(60):
                try:
                    await request(project, {"op": "snapshot"})
                    break
                except (OSError, RuntimeError):
                    await asyncio.sleep(0.2)
            else:
                raise RuntimeError("Supervisor did not start")
            yield process
        finally:
            self.stop(project)
            log.close()

    def stop(self, project: Path) -> None:
        process = self.processes.pop(project, None)
        if process is None or process.poll() is not None:
            return
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=10)

    def leftovers(self) -> list[str]:
        return [
            f"Supervisor {process.pid} is still running"
            for process in self.started
            if process.poll() is None
        ]


class Recording:
    def __init__(self, name: str, renderer: FrameRenderer, project: Path):
        self.name = name
        self.renderer = renderer
        self.hidden = {str(project): PATH_PLACEHOLDER}
        self.forbidden = (str(Path.home()), getpass.getuser(), str(project.parent))
        self.frames: list = []

    def capture(self, client: "Client", caption: str, hold: float = 1.8) -> None:
        ui = self.renderer.ui_image(client.app, self.hidden, self.forbidden)
        self.frames.append(
            (self.renderer.compose(ui, client.label, caption), int(hold * 1000))
        )

    def save(self, directory: Path) -> Path:
        path = directory / f"{self.name}.gif"
        save_gif(self.frames, path)
        return path


class Client:
    def __init__(
        self,
        label: str,
        project: Path,
        recording: Recording,
        size: tuple[int, int] = TERMINAL_SIZE,
    ):
        self.label = label
        self.project = project
        self.recording = recording
        self.size = size

    async def __aenter__(self) -> "Client":
        from kanbeat.ui.app import KanbeatApp

        self.app = KanbeatApp(self.project)
        self.context = self.app.run_test(size=self.size, notifications=True)
        self.pilot = await self.context.__aenter__()
        await self.settle(1.0)
        return self

    async def __aexit__(self, *exc) -> None:
        await self.context.__aexit__(*exc)

    async def close(self) -> None:
        await self.__aexit__(None, None, None)

    async def settle(self, seconds: float = 0.5) -> None:
        await asyncio.sleep(seconds)
        await self.pilot.pause()

    async def click(self, selector: str, settle: float = 0.5) -> None:
        await self.pilot.click(selector)
        await self.settle(settle)

    async def press(self, *keys: str, settle: float = 0.4) -> None:
        await self.pilot.press(*keys)
        await self.settle(settle)

    async def type(self, text: str, settle: float = 0.4) -> None:
        await self.pilot.press(*("space" if char == " " else char for char in text))
        await self.settle(settle)

    async def choose(self, selector: str, steps: int, key: str = "down") -> None:
        await self.click(selector)
        await self.press(*[key] * steps, "enter")

    def shot(self, caption: str, hold: float = 1.8) -> None:
        self.recording.capture(self, caption, hold)


async def request(project: Path, message: dict) -> dict:
    from kanbeat.supervisor import request as supervisor_request

    return await supervisor_request(project, message)


async def board_demo(demo: Demo, renderer: FrameRenderer) -> Recording:
    project = demo.project("board-demo")
    recording = Recording("board", renderer, project)
    async with demo.supervisor(project):
        async with Client("Terminal 1 · TUI", project, recording) as one:
            one.shot(
                "Empty project. The supervisor runs in the background; the board is stopped.",
                2.2,
            )
            await one.click("#new-ticket")
            await one.click("#title")
            await one.type("Add login endpoint")
            one.shot(
                "+ Ticket: type a title and choose where the new ticket starts.", 1.8
            )
            await one.choose("#column", 1)
            one.shot("Initial column: TODO.", 1.4)
            await one.click("#save")
            assert len(demo.tickets(project)["todo"]) == 1
            one.shot("Create writes a plain Markdown file into .kanbeat/todo/.", 2.0)
            demo.cli(
                project,
                "new",
                "Write API docs",
                "--description",
                "Document the public routes.",
                "--column",
                "backlog",
            )
            await one.settle(2.0)
            one.shot(
                'Terminal 2: kanbeat new "Write API docs" ... --column backlog. The open TUI picks it up.',
                2.4,
            )
            await one.click("#view-001")
            one.shot(
                "A ticket is just a file: order-id-slug.md. The TUI shows it read-only.",
                2.4,
            )
            await one.click("#close")
            await one.click("#act-002")
            await one.choose("#column", 1)
            one.shot("Ticket actions: move the card to TODO by hand.", 1.6)
            await one.click("#move")
            assert len(demo.tickets(project)["todo"]) == 2
            one.shot(
                "Manual move: Backlog to Todo. The Markdown file moved to .kanbeat/todo/.",
                2.2,
            )
            await one.click("#act-002")
            await one.click("#up")
            one.shot("Order up: swaps the numeric prefixes of the two files.", 2.2)
            for column, caption in (
                (
                    "progress",
                    "Manual move to Progress. The board is stopped, so no agent runs.",
                ),
                (
                    "review",
                    "Manual move to Review/QA. Nothing here was done by an agent.",
                ),
                (
                    "done",
                    "Manual move to Done. Play would let agents move tickets; this demo does not.",
                ),
            ):
                await one.click("#act-001")
                await one.choose("#column", 1)
                await one.click("#move")
                assert len(demo.tickets(project)[column]) == 1
                one.shot(caption, 2.4)
    return recording


async def snapshot(project: Path) -> dict:
    return await request(project, {"op": "snapshot"})


async def play_stop_demo(demo: Demo, renderer: FrameRenderer) -> Recording:
    project = demo.project("play-demo")
    recording = Recording("play-stop", renderer, project)
    async with demo.supervisor(project):
        demo.cli(
            project,
            "new",
            "Draft release notes",
            "--description",
            "Collect the changes since the last tag.",
            "--column",
            "backlog",
        )
        one = Client("Terminal 1 · TUI", project, recording)
        await one.__aenter__()
        one.shot("Board stopped. Todo is empty, so Play will not start any agent.", 2.2)
        await one.click("#execution", 1.8)
        assert (await snapshot(project))["execution"] == "running"
        one.shot(
            "Play: dispatch is enabled. With an empty Todo column no CLI is started.",
            2.4,
        )
        await one.click("#nav-sessions")
        await one.click("CollapsibleTitle")
        one.shot("While the board runs, task terminals are disabled.", 2.4)
        two = Client("Terminal 2 · TUI", project, recording)
        await two.__aenter__()
        two.shot(
            "A second TUI joins the same supervisor and already shows the running board.",
            2.4,
        )
        await one.close()
        await two.settle(1.8)
        two.shot(
            "Terminal 1 was closed. The supervisor and the board keep running.", 2.4
        )
        await two.click("#execution", 1.8)
        assert (await snapshot(project))["execution"] == "stopped"
        two.shot(
            "Stop: board-owned CLI processes are interrupted (none were running). Supervisor stays alive.",
            2.6,
        )
        await two.click("#nav-sessions")
        await two.click("CollapsibleTitle")
        two.shot("Stopped again: task terminals are available.", 2.4)
        await two.close()
        state = await snapshot(project)
        assert not any(
            (ticket.get("state") or {}).get("runtime") for ticket in state["tickets"]
        )
        assert not (project / ".kanbeat" / ".state" / "logs").exists()
    return recording


async def sessions_demo(demo: Demo, renderer: FrameRenderer) -> Recording:
    project = demo.project("sessions-demo")
    recording = Recording("sessions", renderer, project)
    size = (128, 46)
    async with demo.supervisor(project):
        async with Client("Terminal 1 · TUI", project, recording, size) as one:
            await one.click("#nav-sessions")
            one.shot(
                "Sessions: independent native terminals, organised in your own groups.",
                2.2,
            )
            await one.click("#new-group")
            await one.click("#group-name")
            await one.type("Research")
            one.shot("New group.", 1.4)
            await one.click("#group-save")
            await one.click("#new-group")
            await one.click("#group-name")
            await one.type("Docs")
            await one.click("#group-save")
            groups = (await snapshot(project))["custom"]["groups"]
            assert sorted(row["name"] for row in groups.values()) == [
                "Docs",
                "Research",
            ]
            one.shot(
                "Two groups created. Group names are local organisation only.", 2.0
            )
            await one.click("#new-session")
            await one.click("#session-title")
            await one.type("Notes shell")
            await one.choose("#session-group", 1)
            one.shot(
                "New session: name, group and native harness. The harness here is a stand-in script, not a model.",
                3.0,
            )
            await one.click("#session-start", 1.5)
            one.shot(
                "Opened: a PTY owned by the supervisor. Stop and Detach are in the header.",
                2.4,
            )
            await one.type("hello from terminal 1")
            await one.press("enter", settle=0.8)
            one.shot(
                "Keys travel through the supervisor to the PTY and the screen comes back.",
                2.6,
            )
            await one.press("ctrl+backslash")
            terminals = (await snapshot(project))["terminals"]
            assert [entry["status"] for entry in terminals.values()] == ["running"]
            one.shot(
                "Detach (Ctrl+\\): the session stays running under its group.", 2.6
            )
            await one.click("#execution")
            await one.click("#execution")
            terminals = (await snapshot(project))["terminals"]
            assert [entry["status"] for entry in terminals.values()] == ["running"]
            one.shot("Board Play then Stop: independent sessions are not touched.", 2.6)
            async with Client("Terminal 2 · TUI", project, recording, size) as two:
                await two.click("#nav-sessions")
                two.shot(
                    "Another TUI on the same supervisor sees the same group and session.",
                    2.4,
                )
                terminal_id = next(iter(terminals))
                await two.click(f"#open-session-{terminal_id}", 1.2)
                two.shot(
                    "Open: the earlier output is still on the shared terminal screen.",
                    2.6,
                )
                await two.type("hello from terminal 2")
                await two.press("enter", settle=0.8)
                two.shot("Terminal 2 types into the same process.", 2.4)
                await two.press("ctrl+backslash")
                await two.click(f"#session-actions-{terminal_id}")
                two.shot(
                    "Actions: rename, move between groups, stop, restart or remove the record.",
                    2.6,
                )
                await two.click("#session-stop")
                two.shot(
                    "Stopping an independent session always asks for confirmation.", 2.4
                )
                await two.click("#yes", 1.2)
                terminals = (await snapshot(project))["terminals"]
                assert [entry["status"] for entry in terminals.values()] == ["stopped"]
                two.shot(
                    "Session stopped. Its record, group and output log remain.", 2.6
                )
                await two.click(f"#session-actions-{terminal_id}")
                await two.click("#session-remove")
                await two.click("#yes", 1.0)
                assert not (await snapshot(project))["terminals"]
                two.shot(
                    "Remove record: the stopped session disappears; vendor history is untouched.",
                    2.6,
                )
    return recording


SCENARIOS = {
    "board": board_demo,
    "play-stop": play_stop_demo,
    "sessions": sessions_demo,
}


async def run(arguments: argparse.Namespace) -> None:
    demo = Demo(arguments.workdir)
    demo.prepare()
    renderer = FrameRenderer(
        font_file(arguments.mono_font),
        font_file(arguments.mono_font + ":bold"),
        font_file(arguments.sans_font + ":bold"),
    )
    selected = arguments.only or list(SCENARIOS)
    try:
        for name in selected:
            started = time.monotonic()
            recording = await SCENARIOS[name](demo, renderer)
            path = recording.save(arguments.output)
            print(
                f"{name}: {len(recording.frames)} frames, {path.stat().st_size // 1024} KiB, {time.monotonic() - started:.0f}s"
            )
    finally:
        for project in list(demo.processes):
            demo.stop(project)
    remaining = demo.leftovers()
    if remaining:
        raise SystemExit("Leftover demo processes:\n" + "\n".join(remaining))


def main() -> None:
    os.environ.pop("NO_COLOR", None)
    parser = argparse.ArgumentParser(
        description="Record README demo GIFs from the real Kanbeat TUI"
    )
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("docs/media"))
    parser.add_argument("--only", action="append", choices=sorted(SCENARIOS))
    parser.add_argument("--mono-font", default="Adwaita Mono")
    parser.add_argument("--sans-font", default="Noto Sans")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()

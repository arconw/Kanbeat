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
BOARD_SIZE = (196, 32)
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
    def __init__(self, workdir: Path, real_agents: bool = False):
        self.workdir = workdir.resolve()
        self.bin = self.workdir / "bin"
        self.real_agents = real_agents
        self.processes: dict[Path, subprocess.Popen] = {}
        self.started: list[subprocess.Popen] = []
        self.env = {
            "PATH": self.path(),
            "HOME": os.environ["HOME"],
            "LANG": "C.UTF-8",
            "TERM": "xterm-256color",
            "SHELL": "/bin/sh",
            "TMPDIR": str(self.workdir),
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    def path(self) -> str:
        if not self.real_agents:
            return f"{self.bin}:/usr/bin:/bin"
        found = {}
        for name in HARNESS_NAMES:
            location = shutil.which(name)
            if location is None:
                raise SystemExit(f"The {name} CLI is not on PATH")
            found[str(Path(location).parent)] = None
        return ":".join([*found, "/usr/bin", "/bin"])

    def prepare(self) -> None:
        marker = self.workdir / MARKER
        if self.workdir.exists() and not marker.exists():
            raise SystemExit(
                f"Refusing to reuse a directory I did not create: {self.workdir}"
            )
        self.workdir.mkdir(parents=True, exist_ok=True)
        marker.touch()
        self.bin.mkdir(exist_ok=True)
        for name in () if self.real_agents else HARNESS_NAMES:
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
    def __init__(
        self,
        name: str,
        renderer: FrameRenderer,
        project: Path,
        masks: dict[str, str] | None = None,
        forbidden: tuple[str, ...] = (),
    ):
        self.name = name
        self.renderer = renderer
        home = str(Path.home())
        shown = "~" + str(project)[len(home) :] if str(project).startswith(home) else ""
        self.hidden = {
            str(project): PATH_PLACEHOLDER,
            **({shown: PATH_PLACEHOLDER} if shown else {}),
            **(masks or {}),
        }
        self.forbidden = (
            home,
            getpass.getuser(),
            str(project.parent),
            *forbidden,
        )
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
        async with Client("Terminal 1 · TUI", project, recording, BOARD_SIZE) as one:
            await one.click("#nav-board")
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
            demo.cli(
                project,
                "new",
                "Publish the docs site",
                "--description",
                "Blocked by: #002",
                "--column",
                "backlog",
            )
            await one.settle(2.0)
            one.shot(
                "Dependencies live in the Markdown: a line 'Blocked by: #002'. The cards show who blocks whom.",
                3.0,
            )
            await one.click("#view-003")
            one.shot(
                "The open ticket lists BLOCKED BY (and BLOCKS) like an issue tracker.",
                2.6,
            )
            await one.click(".dependency")
            one.shot("Each entry is a link: it opens the blocking ticket.", 2.4)
            await one.click("#close")
            await one.click("#menu-002")
            one.shot(
                "Each card has [=] [<] [>]: a column menu (Archive last), move left, move right.",
                2.6,
            )
            await one.click("#jump-todo")
            assert len(demo.tickets(project)["todo"]) == 2
            one.shot(
                "Backlog to Todo moves the Markdown file at once. Todo tickets start when their blockers are Done.",
                3.0,
            )
            for column, caption in (
                (
                    "progress",
                    "Any other manual move asks first. The board is stopped, so no agent runs.",
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
                await one.click("#right-001")
                if column == "progress":
                    one.shot(caption, 2.4)
                await one.click("#yes")
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
        await one.click("#nav-board")
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
        await two.click("#nav-board")
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


def terminal_titled(state: dict, title: str) -> str:
    return next(
        key for key, entry in state["terminals"].items() if entry["title"] == title
    )


async def sessions_demo(demo: Demo, renderer: FrameRenderer) -> Recording:
    project = demo.project("sessions-demo")
    recording = Recording("sessions", renderer, project)
    size = (128, 46)
    async with demo.supervisor(project):
        async with Client("Terminal 1 · TUI", project, recording, size) as one:
            one.shot(
                "Sessions is the first tab: groups and sessions on the left, the terminal on the right.",
                2.6,
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
            research = next(k for k, v in groups.items() if v["name"] == "Research")
            one.shot(
                "Two groups created. Group names are local organisation only.", 2.0
            )
            for title, steps in (("Notes shell", 1), ("Scratch pad", 2)):
                await one.click("#new-session")
                await one.click("#session-title")
                await one.type(title)
                await one.choose("#session-group", steps)
                one.app.screen.query_one("#session-harness").value = "claude"
                await one.settle(0.3)
                if title == "Notes shell":
                    one.shot(
                        "New session: name, group and native harness. The harness here is a stand-in script, not a model.",
                        3.0,
                    )
                await one.click("#session-start", 1.5)
                if title == "Notes shell":
                    one.shot(
                        "Opened beside the list. Live, Stop and Detach are in the top bar; the title sits next to the logo.",
                        2.8,
                    )
                    await one.type("hello from terminal 1")
                    await one.press("enter", settle=0.8)
                    one.shot(
                        "Keys travel through the supervisor to the PTY and the screen comes back.",
                        2.6,
                    )
                else:
                    one.shot(
                        "A second session. The first one keeps running in the background.",
                        2.6,
                    )
            (project / "src").mkdir()
            await one.click("#new-session")
            await one.click("#session-title")
            await one.type("Shell")
            one.app.screen.query_one("#session-harness").value = "shell"
            await one.settle(0.3)
            one.shot(
                "A session can also be a plain terminal. Choosing Terminal hides the harness-only fields.",
                3.0,
            )
            await one.click("#session-start", 1.5)
            await one.type("cd src")
            await one.press("enter", settle=0.6)
            await one.type("pwd")
            await one.press("enter", settle=1.8)
            one.shot(
                "A terminal runs your shell. The supervisor remembers its working directory.",
                3.0,
            )
            state = await snapshot(project)
            shell = terminal_titled(state, "Shell")
            notes = terminal_titled(state, "Notes shell")
            assert {entry["status"] for entry in state["terminals"].values()} == {
                "running"
            }
            await one.click(f"#session-row-{notes}")
            await one.settle(0.8)
            one.shot(
                "Click any session to switch to it. The row shows a status square and its own menu.",
                2.8,
            )
            await one.click(f"#group-actions-{research}")
            one.shot(
                "Group menu: rename or remove. An outside click or Esc closes it.", 2.4
            )
            await one.press("escape")
            await one.press("ctrl+backslash")
            terminals = (await snapshot(project))["terminals"]
            assert {entry["status"] for entry in terminals.values()} == {"running"}
            one.shot(
                "Detach (Ctrl+\\): both sessions stay running under their groups.", 2.6
            )
            await one.click("#execution")
            await one.click("#execution")
            terminals = (await snapshot(project))["terminals"]
            assert {entry["status"] for entry in terminals.values()} == {"running"}
            one.shot("Board Play then Stop: independent sessions are not touched.", 2.6)
            async with Client("Terminal 2 · TUI", project, recording, size) as two:
                two.shot(
                    "Another TUI on the same supervisor sees the same groups and sessions.",
                    2.4,
                )
                await two.click(f"#open-session-{notes}", 1.2)
                two.shot(
                    "Open: the earlier output is still on the shared terminal screen.",
                    2.6,
                )
                await two.type("hello from terminal 2")
                await two.press("enter", settle=0.8)
                two.shot("Terminal 2 types into the same process.", 2.4)
                await two.click("#terminal-stop", 1.2)
                terminals = (await snapshot(project))["terminals"]
                assert terminals[notes]["status"] == "stopped"
                two.shot(
                    "Stop in the top bar interrupts the agent at once, with no confirmation. The session stays listed.",
                    3.0,
                )
                await two.click(f"#session-row-{notes}", 1.5)
                terminals = (await snapshot(project))["terminals"]
                assert terminals[notes]["status"] == "running"
                two.shot(
                    "Clicking a stopped session resumes it under the same native session id.",
                    3.0,
                )
                await two.click(f"#open-session-{shell}", 1.2)
                await two.click("#terminal-stop", 1.5)
                assert (await snapshot(project))["terminals"][shell]["status"] == (
                    "stopped"
                )
                await two.click(f"#session-row-{shell}", 1.5)
                await two.type("pwd")
                await two.press("enter", settle=1.2)
                two.shot(
                    "Stop the terminal and click it again: a fresh shell opens in the same directory.",
                    3.2,
                )
                await two.press("ctrl+backslash")
                await two.click(f"#session-actions-{notes}")
                two.shot(
                    "Session menu: Edit, Stop, Remove from Kanbeat, and Delete session and history.",
                    3.0,
                )
                await two.click("#session-stop")
                await two.click(f"#session-actions-{notes}")
                await two.click("#session-remove")
                await two.click("#yes", 1.0)
                assert notes not in (await snapshot(project))["terminals"]
                two.shot(
                    "Remove from Kanbeat drops the record; the conversation stays in the harness.",
                    2.8,
                )
    return recording


AGENT_TASKS = (
    (
        "claude",
        "Claude Code",
        "average.py",
        "def average(numbers):\n    return sum(numbers) / (len(numbers) + 1)\n",
        "test_average.py",
        "import unittest\nfrom average import average\n\n\nclass AverageTest(unittest.TestCase):\n"
        "    def test_values(self):\n        self.assertEqual(average([2, 4, 6]), 4)\n"
        "        self.assertEqual(average([10]), 10)\n\n\nif __name__ == '__main__':\n    unittest.main()\n",
        "The test in test_average.py fails. Find the bug in average.py, fix it and run the test to confirm.",
    ),
    (
        "opencode",
        "OpenCode",
        "fizzbuzz.py",
        "def fizzbuzz(n):\n    if n % 3 == 0:\n        return 'Fizz'\n    if n % 5 == 0:\n        return 'Buzz'\n"
        "    if n % 15 == 0:\n        return 'FizzBuzz'\n    return str(n)\n",
        "test_fizzbuzz.py",
        "import unittest\nfrom fizzbuzz import fizzbuzz\n\n\nclass FizzBuzzTest(unittest.TestCase):\n"
        "    def test_fifteen(self):\n        self.assertEqual(fizzbuzz(15), 'FizzBuzz')\n\n\n"
        "if __name__ == '__main__':\n    unittest.main()\n",
        "fizzbuzz(15) returns the wrong word. Fix fizzbuzz.py and run test_fizzbuzz.py.",
    ),
    (
        "pi",
        "Pi",
        "clamp.py",
        "def clamp(value, low, high):\n    return max(high, min(value, low))\n",
        "test_clamp.py",
        "import unittest\nfrom clamp import clamp\n\n\nclass ClampTest(unittest.TestCase):\n"
        "    def test_inside(self):\n        self.assertEqual(clamp(5, 0, 10), 5)\n"
        "        self.assertEqual(clamp(-3, 0, 10), 0)\n\n\nif __name__ == '__main__':\n    unittest.main()\n",
        "clamp(5, 0, 10) should be 5 but the test fails. Fix clamp.py and run test_clamp.py.",
    ),
    (
        "codex",
        "Codex",
        "slugify.py",
        "def slugify(text):\n    return text.lower().replace(' ', '-')\n",
        "test_slugify.py",
        "import unittest\nfrom slugify import slugify\n\n\nclass SlugifyTest(unittest.TestCase):\n"
        "    def test_punctuation(self):\n        self.assertEqual(slugify('Hello, World!'), 'hello-world')\n\n\n"
        "if __name__ == '__main__':\n    unittest.main()\n",
        "test_slugify.py fails because slugify keeps punctuation. Fix slugify.py and run the test.",
    ),
)
TRUST_ANSWERS = (
    ("Yes, I trust this folder", ("down", "enter")),
    ("Do you trust", ("enter",)),
    ("trust the contents", ("enter",)),
)


async def screen_text(project: Path, terminal: str) -> str:
    return (await request(project, {"op": "terminal_snapshot", "terminal": terminal}))[
        "text"
    ]


async def settled(
    project: Path,
    terminal: str,
    quiet: float = 6.0,
    minimum: float = 4.0,
    timeout: float = 240.0,
) -> str:
    started = time.monotonic()
    last, changed = "", time.monotonic()
    while time.monotonic() - started < timeout:
        text = await screen_text(project, terminal)
        if text != last:
            last, changed = text, time.monotonic()
        elif (
            time.monotonic() - changed >= quiet
            and time.monotonic() - started >= minimum
        ):
            return text
        await asyncio.sleep(1.0)
    raise RuntimeError("Terminal did not settle within the timeout")


async def say(one: "Client", text: str, typed: bool = False) -> None:
    from textual import events
    from kanbeat.ui.terminal import TerminalDisplay

    if typed:
        await one.type(text, settle=0.8)
    else:
        one.app.screen.query_one(TerminalDisplay).post_message(events.Paste(text))
        await one.settle(0.8)
    await one.press("enter", settle=0.8)


async def agents_demo(
    demo: Demo,
    renderer: FrameRenderer,
    masks: dict[str, str] | None = None,
    forbidden: tuple[str, ...] = (),
    agents: tuple[str, ...] = (),
) -> Recording:
    tasks = [task for task in AGENT_TASKS if not agents or task[0] in agents]
    project = demo.project("agents-demo")
    for _, _, source, code, test, test_code, _ in tasks:
        (project / source).write_text(code, encoding="utf-8")
        (project / test).write_text(test_code, encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=project, check=True)
    recording = Recording("agents", renderer, project, masks, forbidden)
    size = (128, 46)
    try:
        async with demo.supervisor(project):
            async with Client("Terminal 1 · TUI", project, recording, size) as one:
                await one.click("#new-group")
                await one.click("#group-name")
                await one.type("Fixes")
                await one.click("#group-save")
                ids: dict[str, str] = {}
                for harness, label, source, code, test, _, prompt in tasks:
                    await one.click("#new-session")
                    await one.click("#session-title")
                    await one.type(f"Fix {source}")
                    await one.choose("#session-group", 1)
                    one.app.screen.query_one("#session-harness").value = harness
                    await one.settle(0.3)
                    await one.click("#session-start", 2.0)
                    state = await snapshot(project)
                    terminal = next(
                        key
                        for key, entry in state["terminals"].items()
                        if entry["title"] == f"Fix {source}"
                    )
                    ids[harness] = terminal
                    text = await settled(project, terminal, quiet=4.0, minimum=6.0)
                    for phrase, keys in TRUST_ANSWERS:
                        if phrase in text:
                            await one.press(*keys, settle=1.0)
                            text = await settled(
                                project, terminal, quiet=4.0, minimum=4.0
                            )
                            break
                    one.shot(f"{label} starts in its own native terminal.", 2.4)
                    for attempt in range(3):
                        await say(one, prompt, typed=attempt == 2)
                        for _ in range(45):
                            if (project / source).read_text(encoding="utf-8") != code:
                                break
                            await asyncio.sleep(1.0)
                        else:
                            continue
                        break
                    else:
                        import json

                        info = await request(
                            project, {"op": "terminal_snapshot", "terminal": terminal}
                        )
                        keep = {
                            key: value
                            for key, value in info.items()
                            if key not in ("screen", "text")
                        }
                        (demo.workdir / f"agents-{harness}-debug.json").write_text(
                            json.dumps(keep, indent=1, default=str), encoding="utf-8"
                        )
                    await one.settle(1.0)
                    one.shot(
                        f"{label} reads the code, edits it and runs the test. Its own UI, its own account.",
                        2.6,
                    )
                for harness, label, source, code, *_ in tasks:
                    for _ in range(240):
                        if (project / source).read_text(encoding="utf-8") != code:
                            break
                        await asyncio.sleep(1.0)
                    await settled(project, ids[harness], quiet=10.0, minimum=5.0)
                    (demo.workdir / f"agents-{harness}.txt").write_text(
                        await screen_text(project, ids[harness]), encoding="utf-8"
                    )
                for harness, label, source, code, test, test_code, prompt in tasks:
                    await one.click(f"#session-row-{ids[harness]}", 1.5)
                    one.shot(
                        f"{label}: finished. Click a session to switch between agents.",
                        3.0,
                    )
                for harness, label, source, code, test, test_code, prompt in tasks:
                    fixed = (project / source).read_text(encoding="utf-8")
                    assert fixed != code, f"{label} did not change {source}"
                await one.press("ctrl+backslash")
                one.shot(
                    "Detach: all four agents stay available under their group.", 2.6
                )
    except Exception:
        recording.name = "agents-failed"
        recording.save(demo.workdir)
        raise
    return recording


CHANGES_FILES = {
    ".gitignore": ".kanbeat/\n",
    "src/shapes.py": (
        "def area(width, height):\n"
        "    return width + height\n"
        "\n"
        "\n"
        "def perimeter(width, height):\n"
        "    return 2 * (width + height)\n"
        "\n"
        "\n"
        "def describe(width, height):\n"
        "    return f'{width} x {height}'\n"
    ),
    "src/cli.py": (
        "import sys\n"
        "\n"
        "from shapes import area\n"
        "\n"
        "\n"
        "def main():\n"
        "    print(area(2, 3))\n"
        "\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    sys.exit(main())\n"
    ),
    "tests/test_shapes.py": (
        "from shapes import area\n\n\ndef test_area():\n    assert area(2, 3) == 5\n"
    ),
    "README.md": "# Shapes\n\nSmall geometry helpers.\n",
}


def demo_git(demo: Demo, project: Path, *arguments: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Demo",
            "-c",
            "user.email=demo@example.com",
            "-c",
            "init.defaultBranch=main",
            *arguments,
        ],
        cwd=project,
        env={**demo.env, "GIT_CONFIG_GLOBAL": "/dev/null"},
        check=True,
        capture_output=True,
    )


def prepare_changes_project(demo: Demo, project: Path) -> None:
    for name, content in CHANGES_FILES.items():
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    demo_git(demo, project, "init", "-q")
    demo_git(demo, project, "add", ".")
    demo_git(demo, project, "commit", "-q", "-m", "Start")
    shapes = project / "src" / "shapes.py"
    shapes.write_text(
        shapes.read_text(encoding="utf-8").replace(
            "return width + height\n\n\ndef perimeter",
            "return width * height\n\n\ndef perimeter",
        )
        + "\n\ndef scale(width, height, factor):\n"
        "    return width * factor, height * factor\n",
        encoding="utf-8",
    )
    cli = project / "src" / "cli.py"
    cli.write_text(
        cli.read_text(encoding="utf-8").replace(
            "print(area(2, 3))", "print(area(2, 3))\n    return 0"
        ),
        encoding="utf-8",
    )
    tests = project / "tests" / "test_shapes.py"
    tests.write_text(
        tests.read_text(encoding="utf-8").replace("== 5", "== 6"), encoding="utf-8"
    )
    (project / "docs").mkdir()
    (project / "docs" / "notes.md").write_text(
        "# Notes\n\nArea is width times height.\nPerimeter stays as it is.\n",
        encoding="utf-8",
    )
    demo_git(demo, project, "add", "src/cli.py")


async def changes_demo(demo: Demo, renderer: FrameRenderer) -> Recording:
    from kanbeat.ui.diff import DiffView

    project = demo.project("changes-demo")
    prepare_changes_project(demo, project)
    recording = Recording("changes", renderer, project)
    size = (160, 40)
    async with demo.supervisor(project):
        async with Client("Terminal 1 · TUI", project, recording, size) as one:
            await one.app.refresh_changes(True)
            await one.settle(0.8)
            button = one.app.query_one("#diff-toggle")
            assert button.label.plain.startswith("4 files")
            one.shot(
                "In a Git project the files button counts the changed files and lines. The panel is closed until you open it.",
                3.0,
            )
            await one.click("#new-session")
            await one.click("#session-title")
            await one.type("Notes shell")
            one.app.screen.query_one("#session-harness").value = "claude"
            await one.settle(0.3)
            await one.click("#session-start", 1.5)
            await one.click("#diff-toggle", 1.0)
            await one.app.refresh_changes(True)
            await one.settle(1.0)
            view = one.app.query_one(DiffView)
            assert view.display and "src/shapes.py" in view.canvas.offsets
            one.shot(
                "Click files: the changes open beside the terminal. Files on top, every diff below; the session keeps running.",
                3.4,
            )
            names = [row.path for row in view.files.rows if row.action == "jump"]
            await one.pilot.click(view.files, offset=(3, names.index("src/shapes.py")))
            await one.settle(0.6)
            one.shot(
                "Click a file in the list to jump to it. The list and the diff scroll separately.",
                3.0,
            )
            canvas = view.canvas
            header = canvas.offsets["docs/notes.md"]
            canvas.scroll_to(y=max(0, header - 3), animate=False, immediate=True)
            await one.settle(0.5)
            one.shot(
                "New files start closed. Tests and generated files hide behind a (show) row.",
                3.0,
            )
            await one.pilot.click(canvas, offset=(3, header - canvas.scroll_offset.y))
            await one.settle(0.6)
            assert "docs/notes.md" in canvas.opened_new
            one.shot("Click a header to open a new file.", 2.4)
            target = canvas.offsets["src/shapes.py"]
            canvas.scroll_to(y=max(0, target - 1), animate=False, immediate=True)
            await one.settle(0.5)
            ask = canvas.rows[target].ask_from
            await one.pilot.click(
                canvas, offset=(ask + 2, target - canvas.scroll_offset.y)
            )
            await one.settle(1.0)
            state = await snapshot(project)
            terminal = terminal_titled(state, "Notes shell")
            screen = await request(
                project, {"op": "terminal_snapshot", "terminal": terminal}
            )
            assert "@src/shapes.py" in screen["text"]
            one.shot(
                "[ ask ] types a reference to the file into the open session without sending it.",
                3.4,
            )
            await one.click("#diff-source", 0.6)
            one.shot("Source: Current, Unstaged, Staged or Last commit.", 2.6)
            await one.click("#source-staged", 1.2)
            assert [row.path for row in view.files.rows if row.action == "jump"] == [
                "src/cli.py"
            ]
            one.shot("Staged shows only what is already added to the index.", 2.8)
            await one.click("#diff-source", 0.5)
            await one.click("#source-current", 1.2)
            await one.click("#diff-close", 0.8)
            assert not view.display
            one.shot(
                "Close it with x or the files button. Kanbeat remembers whether you left it open.",
                2.8,
            )
    return recording


SCENARIOS = {
    "board": board_demo,
    "play-stop": play_stop_demo,
    "sessions": sessions_demo,
    "changes": changes_demo,
    "agents": agents_demo,
}
REAL_SCENARIOS = {"agents"}


async def run(arguments: argparse.Namespace) -> None:
    selected = arguments.only or [
        name for name in SCENARIOS if name not in REAL_SCENARIOS
    ]
    demo = Demo(arguments.workdir, real_agents=bool(REAL_SCENARIOS & set(selected)))
    demo.prepare()
    renderer = FrameRenderer(
        font_file(arguments.mono_font),
        font_file(arguments.mono_font + ":bold"),
        font_file(arguments.sans_font + ":bold"),
    )
    masks = dict(item.split("=", 1) for item in arguments.mask)
    try:
        for name in selected:
            started = time.monotonic()
            if name in REAL_SCENARIOS:
                recording = await SCENARIOS[name](
                    demo,
                    renderer,
                    masks,
                    tuple(arguments.forbid),
                    tuple(arguments.agent),
                )
            else:
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
    parser.add_argument(
        "--mask",
        action="append",
        default=[],
        metavar="TEXT=REPLACEMENT",
        help="rewrite text in every frame (real-agent demos)",
    )
    parser.add_argument(
        "--agent",
        action="append",
        default=[],
        choices=[task[0] for task in AGENT_TASKS],
        help="record only these agents in the real-agent demo",
    )
    parser.add_argument(
        "--forbid",
        action="append",
        default=[],
        metavar="TEXT",
        help="refuse to render a frame that still contains TEXT",
    )
    parser.add_argument("--mono-font", default="Adwaita Mono")
    parser.add_argument("--sans-font", default="Noto Sans")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()

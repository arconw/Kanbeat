import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from textual.widgets import Button

from kanbeat.diff import Changes, FileDiff, collect_changes
from kanbeat.supervisor import Supervisor
from kanbeat.ui.app import DIFF_HIDDEN_KEY, KanbeatApp, SourceMenu, file_reference
from kanbeat.ui.diff import DiffView
from kanbeat.ui.sessions import SessionsView
from kanbeat.ui.terminal import TerminalPane

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is required")

LONG = "".join(f"line {number}\n" for number in range(1, 80))


def git(path: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=demo", *args],
        cwd=path,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text(".kanbeat/\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (tmp_path / "src" / "long.txt").write_text(LONG)
    (tmp_path / "README.md").write_text("# demo\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "init")
    (tmp_path / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (tmp_path / "src" / "long.txt").write_text(
        LONG.replace("line 70\n", "changed 70\n")
    )
    (tmp_path / "notes.txt").write_text("one\ntwo\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text("def test_add():\n    assert 1\n")
    return tmp_path


def make_app(path, monkeypatch):
    supervisor = Supervisor(path)
    app = KanbeatApp(path)
    monkeypatch.setattr(app, "rpc", supervisor.dispatch)
    return app


async def opened(app, pilot):
    await pilot.pause()
    await app.refresh_changes(True)
    view = app.query_one(DiffView)
    if not view.display:
        await click_button(pilot, "#diff-toggle")
    await until(pilot, lambda: view.display and view.changes is not None)
    await pilot.pause()
    return view


async def click_button(pilot, selector):
    await until(pilot, lambda: not pilot.app.query_one(selector).has_class("-active"))
    await pilot.click(selector)


async def until(pilot, condition):
    for _ in range(150):
        if condition():
            return
        await pilot.pause(0.02)
    assert condition()


def texts(view):
    return [row.text.plain for row in view.canvas.rows]


def list_paths(view):
    return [row.path for row in view.files.rows if row.action == "jump"]


@pytest.mark.asyncio
async def test_panel_starts_closed_takes_a_third_when_opened_and_is_remembered(
    repo, monkeypatch
):
    app = make_app(repo, monkeypatch)
    state = repo / ".kanbeat" / ".state" / "ui.json"
    async with app.run_test(size=(170, 40)) as pilot:
        await pilot.pause()
        await pilot.pause()
        view = app.query_one(DiffView)
        button = app.query_one("#diff-toggle", Button)
        assert not view.display and not button.has_class("selected")
        view = await opened(app, pilot)
        page = app.query_one(SessionsView)
        pane = app.query_one(TerminalPane)
        assert 0.27 <= view.region.width / page.region.width <= 0.33
        assert view.region.x >= pane.region.right and pane.region.width > 0
        assert button.label.plain == "4 files | +6 -2" and button.has_class("selected")
    assert json.loads(state.read_text())[DIFF_HIDDEN_KEY] is False
    again = make_app(repo, monkeypatch)
    async with again.run_test(size=(170, 40)) as pilot:
        view = again.query_one(DiffView)
        close = again.query_one("#diff-close", Button)
        await until(pilot, lambda: view.display and close.region.width > 0)
        await click_button(pilot, "#diff-close")
        await until(pilot, lambda: not view.display)
    assert json.loads(state.read_text())[DIFF_HIDDEN_KEY] is True
    third = make_app(repo, monkeypatch)
    async with third.run_test(size=(170, 40)) as pilot:
        await pilot.pause()
        await pilot.pause()
        assert not third.query_one(DiffView).display


@pytest.mark.asyncio
async def test_button_blinks_only_for_changes_the_user_has_not_seen(repo, monkeypatch):
    app = make_app(repo, monkeypatch)
    async with app.run_test(size=(170, 40)) as pilot:
        view = await opened(app, pilot)
        button = app.query_one("#diff-toggle", Button)

        def change(expression):
            (repo / "src" / "calc.py").write_text(
                f"def add(a, b):\n    return {expression}\n"
            )

        change("a * b")
        await app.refresh_changes()
        assert not button.has_class("diff-unseen")
        app._pulse_bright = False
        app.pulse_working()
        assert not button.has_class("diff-pulse")
        await click_button(pilot, "#diff-toggle")
        await until(pilot, lambda: not view.display)
        await app.refresh_changes()
        assert not button.has_class("diff-unseen")
        change("a / b")
        await app.refresh_changes()
        assert button.has_class("diff-unseen")
        app._pulse_bright = False
        app.pulse_working()
        assert button.has_class("diff-pulse")
        app.pulse_working()
        assert not button.has_class("diff-pulse")
        await click_button(pilot, "#diff-toggle")
        await until(pilot, lambda: view.display)
        assert not button.has_class("diff-unseen")
        app._pulse_bright = False
        app.pulse_working()
        assert not button.has_class("diff-pulse")
        await click_button(pilot, "#diff-toggle")
        await until(pilot, lambda: not view.display)
        await app.refresh_changes()
        assert not button.has_class("diff-unseen")
        change("a % b")
        await app.refresh_changes()
        assert button.has_class("diff-unseen")
        change("a / b")
        await app.refresh_changes()
        assert not button.has_class("diff-unseen")


@pytest.mark.asyncio
async def test_a_panel_hidden_from_the_start_blinks_after_the_first_change(
    repo, monkeypatch
):
    state = repo / ".kanbeat" / ".state"
    state.mkdir(parents=True)
    (state / "ui.json").write_text(json.dumps({DIFF_HIDDEN_KEY: True}))
    app = make_app(repo, monkeypatch)
    async with app.run_test(size=(170, 40)) as pilot:
        await pilot.pause()
        await app.refresh_changes(True)
        button = app.query_one("#diff-toggle", Button)
        assert not app.query_one(DiffView).display
        assert not button.has_class("diff-unseen")
        (repo / "b.txt").write_text("new\n")
        await app.refresh_changes()
        assert button.has_class("diff-unseen")


@pytest.mark.asyncio
async def test_panel_is_the_first_to_close_when_the_window_gets_narrow(
    repo, monkeypatch
):
    app = make_app(repo, monkeypatch)
    async with app.run_test(size=(170, 40)) as pilot:
        view = await opened(app, pilot)
        pane = app.query_one(TerminalPane)
        await pilot.resize_terminal(130, 40)
        await until(pilot, lambda: not view.display)
        assert pane.region.width >= 60
        assert not app.diff_hidden()
        await pilot.resize_terminal(170, 40)
        await until(pilot, lambda: view.display)


@pytest.mark.asyncio
async def test_on_a_very_narrow_window_the_panel_covers_the_whole_sessions_page(
    repo, monkeypatch
):
    app = make_app(repo, monkeypatch)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        await app.refresh_changes(True)
        view = app.query_one(DiffView)
        page = app.query_one(SessionsView)
        assert not view.display
        await click_button(pilot, "#diff-toggle")
        await until(pilot, lambda: view.display and view.changes is not None)
        await pilot.pause()
        assert view.has_class("-cover")
        assert view.region.x == page.region.x and view.region.width == page.region.width
        await click_button(pilot, "#diff-toggle")
        await until(pilot, lambda: not view.display)
        assert not app.diff_hidden()
        await pilot.resize_terminal(170, 40)
        await until(pilot, lambda: view.display)
        assert not view.has_class("-cover")


@pytest.mark.asyncio
async def test_button_is_only_on_the_sessions_tab(repo, monkeypatch):
    app = make_app(repo, monkeypatch)
    async with app.run_test(size=(170, 40)) as pilot:
        await pilot.pause()
        assert app.query_one("#diff-toggle", Button).display
        await pilot.click("#nav-board")
        await pilot.pause()
        assert not app.query_one("#diff-toggle", Button).display


@pytest.mark.asyncio
async def test_list_and_stream_show_files_with_tests_hidden_behind_a_group(
    repo, monkeypatch
):
    app = make_app(repo, monkeypatch)
    async with app.run_test(size=(170, 40)) as pilot:
        view = await opened(app, pilot)
        assert list_paths(view) == ["notes.txt", "src/calc.py", "src/long.txt"]
        group = [row for row in view.files.rows if row.action == "group"]
        assert group and group[0].text.plain == "1 tests/generated (show)"
        assert "tests/test_calc.py" not in view.canvas.offsets
        assert "4 files" in view.query_one("#diff-title").render().plain
        await pilot.click(view.files, offset=(3, len(view.files.rows) - 1))
        await pilot.pause()
        await pilot.pause()
        assert "tests/test_calc.py" in view.canvas.offsets
        assert "tests/test_calc.py" in list_paths(view)
        assert view.files.rows[3].text.plain.startswith("1 tests/generated (hide)")


@pytest.mark.asyncio
async def test_new_files_are_closed_but_can_be_opened_and_tracked_files_are_open(
    repo, monkeypatch
):
    app = make_app(repo, monkeypatch)
    async with app.run_test(size=(170, 40)) as pilot:
        view = await opened(app, pilot)
        canvas = view.canvas
        rows = texts(view)
        assert "New file, not staged yet." in rows
        assert not any(text.endswith("one") for text in rows)
        assert any("return a + b" in text for text in rows)
        assert any(text.startswith("  2 -") and "return a - b" in text for text in rows)
        header = canvas.offsets["notes.txt"]
        assert "(untracked)" in canvas.rows[header].text.plain
        assert "[ ask ]" in canvas.rows[header].text.plain
        await pilot.click(canvas, offset=(3, header - canvas.scroll_offset.y))
        await pilot.pause()
        assert any(text.endswith("one") for text in texts(view))
        await pilot.click(canvas, offset=(3, header - canvas.scroll_offset.y))
        await pilot.pause()
        assert "New file, not staged yet." in texts(view)
        tracked = canvas.offsets["src/calc.py"]
        before = len(canvas.rows)
        await pilot.click(canvas, offset=(3, tracked - canvas.scroll_offset.y))
        await pilot.pause()
        assert len(canvas.rows) < before


@pytest.mark.asyncio
async def test_choosing_a_file_in_the_list_scrolls_the_stream_to_it(repo, monkeypatch):
    app = make_app(repo, monkeypatch)
    async with app.run_test(size=(170, 20)) as pilot:
        view = await opened(app, pilot)
        canvas = view.canvas
        assert canvas.scroll_offset.y == 0
        target = canvas.offsets["src/long.txt"]
        assert target > 5
        index = list_paths(view).index("src/long.txt")
        await pilot.click(view.files, offset=(3, index))
        await pilot.pause()
        assert canvas.scroll_offset.y == target - 1
        await pilot.click(view.files, offset=(3, 0))
        await pilot.pause()
        assert canvas.scroll_offset.y == max(0, canvas.offsets["notes.txt"] - 1)


@pytest.mark.asyncio
async def test_both_blocks_scroll_independently(tmp_path, monkeypatch):
    git(tmp_path, "init", "-q")
    for number in range(30):
        (tmp_path / f"file{number:02}.txt").write_text("x\n" * 40)
    app = make_app(tmp_path, monkeypatch)
    async with app.run_test(size=(170, 24)) as pilot:
        view = await opened(app, pilot)
        files, canvas = view.files, view.canvas
        assert files.max_scroll_y > 0
        files.scroll_to(y=3, animate=False, immediate=True)
        await pilot.pause()
        assert files.scroll_offset.y == 3 and canvas.scroll_offset.y == 0
        canvas.scroll_to(y=2, animate=False, immediate=True)
        await pilot.pause()
        assert canvas.scroll_offset.y == 2 and files.scroll_offset.y == 3


@pytest.mark.asyncio
async def test_long_lines_wrap_inside_the_panel(repo, monkeypatch):
    (repo / "wide.py").write_text(
        "x = " + " + ".join(f"value_{n}" for n in range(40)) + "\n"
    )
    app = make_app(repo, monkeypatch)
    async with app.run_test(size=(170, 40)) as pilot:
        view = await opened(app, pilot)
        canvas = view.canvas
        header = canvas.offsets["wide.py"]
        await pilot.click(canvas, offset=(3, header - canvas.scroll_offset.y))
        await pilot.pause()
        start = canvas.offsets["wide.py"] + 2
        width = canvas.content_width
        wrapped = [row.text.plain for row in canvas.rows[start : start + 4]]
        assert wrapped[0].startswith("  1 +x = value_0")
        assert wrapped[1].startswith("     ") and wrapped[1].strip()
        assert all(len(row) <= width for row in wrapped)


@pytest.mark.asyncio
async def test_ask_puts_a_reference_to_the_file_into_the_open_session(
    repo, monkeypatch
):
    app = make_app(repo, monkeypatch)
    async with app.run_test(size=(170, 40)) as pilot:
        view = await opened(app, pilot)
        sent = []

        async def record(values):
            sent.append(values)

        pane = app.query_one(TerminalPane)
        monkeypatch.setattr(pane, "send_input", record)
        canvas = view.canvas
        header = canvas.offsets["src/calc.py"]
        ask_from = canvas.rows[header].ask_from
        await pilot.click(canvas, offset=(ask_from + 2, header))
        await pilot.pause()
        assert sent == []
        assert any("Open a session first" in n.message for n in app._notifications)
        app.embedded_terminal = "t1"
        app.snapshot = {"terminals": {"t1": {"harness": "claude"}}}
        await pilot.click(canvas, offset=(ask_from + 2, header))
        await pilot.pause()
        assert sent == [{"kind": "paste", "text": "@src/calc.py "}]
        app.snapshot = {"terminals": {"t1": {"harness": "shell"}}}
        await pilot.click(canvas, offset=(ask_from + 2, header))
        await pilot.pause()
        assert sent[-1] == {"kind": "paste", "text": "src/calc.py "}
        assert "src/calc.py" not in canvas.closed_tracked


@pytest.mark.asyncio
async def test_source_menu_switches_between_current_staged_and_unstaged(
    repo, monkeypatch
):
    git(repo, "add", "src/calc.py")
    app = make_app(repo, monkeypatch)
    async with app.run_test(size=(170, 40)) as pilot:
        view = await opened(app, pilot)
        assert "source: Current" in str(view.query_one("#diff-source", Button).label)
        await pilot.click("#diff-source")
        await pilot.pause()
        assert isinstance(app.screen, SourceMenu)
        await pilot.click("#source-staged")
        await pilot.pause()
        await pilot.pause()
        assert view.source == "staged"
        assert "source: Staged" in str(view.query_one("#diff-source", Button).label)
        assert list_paths(view) == ["src/calc.py"]
        await pilot.click("#diff-source")
        await pilot.pause()
        await pilot.click("#source-unstaged")
        await pilot.pause()
        await pilot.pause()
        assert list_paths(view) == ["notes.txt", "src/long.txt"]
        await pilot.click("#diff-source")
        await pilot.pause()
        await pilot.click("#source-current")
        await pilot.pause()
        await pilot.pause()
        assert len(list_paths(view)) == 3


@pytest.mark.asyncio
async def test_diff_updates_in_real_time_and_keeps_opened_state(repo, monkeypatch):
    app = make_app(repo, monkeypatch)
    async with app.run_test(size=(170, 40)) as pilot:
        view = await opened(app, pilot)
        view.canvas.opened_new.add("notes.txt")
        view.canvas.closed_tracked.add("src/long.txt")
        (repo / "src" / "calc.py").write_text("def add(a, b):\n    return a * b\n")
        (repo / "extra.txt").write_text("fresh\n")
        await app.refresh_changes()
        await pilot.pause()
        assert any("return a * b" in text for text in texts(view))
        assert "extra.txt" in view.canvas.offsets
        assert any(text.endswith("one") for text in texts(view))
        assert app.query_one("#diff-toggle", Button).label.plain.startswith("5 files")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "all")
        await app.refresh_changes()
        await pilot.pause()
        assert str(app.query_one("#diff-toggle", Button).label) == "files"
        assert "No changes" in texts(view)[0]


@pytest.mark.asyncio
async def test_project_outside_git_explains_itself(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    app = make_app(tmp_path, monkeypatch)
    async with app.run_test(size=(170, 40)) as pilot:
        view = await opened(app, pilot)
        assert "Git repository" in texts(view)[0]
        assert str(app.query_one("#diff-toggle", Button).label) == "files"


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [114, 120, 135, 136, 150, 160, 200])
async def test_navigation_fits_with_terminal_controls_and_big_counts(
    tmp_path, monkeypatch, width
):
    app = make_app(tmp_path, monkeypatch)

    async def idle(force=False):
        return None

    monkeypatch.setattr(app, "refresh_changes", idle)
    async with app.run_test(size=(width, 30)) as pilot:
        await pilot.pause()
        app.changes = Changes(
            available=True,
            files=[FileDiff(path=f"f{n}", additions=3, deletions=1) for n in range(12)],
        )
        app.embedded_terminal = "terminal"
        app.update_diff_button()
        app.query_one("#terminal-controls").display = True
        await pilot.pause()
        await pilot.pause()
        for name in ("#diff-toggle", "#nav-settings", "#execution", "#quit"):
            assert app.query_one(name).region.right <= width, name
        label = app.query_one("#diff-toggle", Button).label.plain
        assert label == ("12 files | +36 -12" if width >= 160 else "12 +36 -12")


@pytest.mark.parametrize(
    "harness,path,expected",
    [
        ("claude", "src/a.py", "@src/a.py"),
        ("codex", "my file.txt", '@"my file.txt"'),
        ("claude", 'we"ird.txt', '@we"ird.txt'),
        ("claude", 'we"ird name.txt', None),
        ("shell", "src/a.py", "src/a.py"),
        ("shell", "my file.txt", "'my file.txt'"),
        ("shell", "$(rm -rf ~).txt", "'$(rm -rf ~).txt'"),
        ("shell", "it's.txt", "'it'\"'\"'s.txt'"),
    ],
)
def test_file_references_are_quoted_for_the_target(harness, path, expected):
    assert file_reference({"harness": harness}, path) == expected


@pytest.mark.asyncio
async def test_ask_refuses_names_with_control_characters(repo, monkeypatch):
    app = make_app(repo, monkeypatch)
    async with app.run_test(size=(170, 40)) as pilot:
        await opened(app, pilot)
        sent = []

        async def record(values):
            sent.append(values)

        monkeypatch.setattr(app.query_one(TerminalPane), "send_input", record)
        app.embedded_terminal = "t1"
        app.snapshot = {"terminals": {"t1": {"harness": "shell"}}}
        app.ask_about("x\nrm -rf ~")
        app.ask_about("bell\x07.txt")
        await pilot.pause()
        assert sent == []
        assert sum("control characters" in n.message for n in app._notifications) == 2


@pytest.mark.asyncio
async def test_a_slow_result_for_the_old_source_is_dropped(repo, monkeypatch):
    app = make_app(repo, monkeypatch)
    async with app.run_test(size=(170, 40)) as pilot:
        view = await opened(app, pilot)
        release = asyncio.Event()
        real = collect_changes

        async def slow(project, previous="", source="current"):
            if source == "current":
                await release.wait()
            return real(project, previous, source)

        monkeypatch.setattr("kanbeat.ui.diff.collect", slow)
        stale = asyncio.create_task(view.poll(force=True))
        await pilot.pause()
        await view.set_source("staged")
        await until(pilot, lambda: view.changes is not None and view.source == "staged")
        staged = list_paths(view)
        release.set()
        await stale
        await pilot.pause()
        assert view.source == "staged"
        assert list_paths(view) == staged == []


@pytest.mark.asyncio
async def test_ask_refuses_names_that_cannot_be_referenced_safely(repo, monkeypatch):
    app = make_app(repo, monkeypatch)
    async with app.run_test(size=(170, 40)) as pilot:
        await opened(app, pilot)
        sent = []

        async def record(values):
            sent.append(values)

        monkeypatch.setattr(app.query_one(TerminalPane), "send_input", record)
        app.embedded_terminal = "t1"
        app.snapshot = {"terminals": {"t1": {"harness": "claude"}}}
        app.ask_about('we"ird name.txt')
        await pilot.pause()
        assert sent == []
        assert any("cannot be referenced" in n.message for n in app._notifications)


@pytest.mark.asyncio
async def test_a_failing_save_does_not_stop_the_panel_from_opening_or_closing(
    repo, monkeypatch
):
    app = make_app(repo, monkeypatch)

    def broken(key, value):
        raise OSError("read-only file system")

    monkeypatch.setattr(app.preferences, "remember", broken)
    async with app.run_test(size=(170, 40)) as pilot:
        view = await opened(app, pilot)
        assert view.display
        await click_button(pilot, "#diff-toggle")
        await until(pilot, lambda: not view.display)
        await click_button(pilot, "#diff-toggle")
        await until(pilot, lambda: view.display)
        await click_button(pilot, "#diff-close")
        await until(pilot, lambda: not view.display)
        saves = [n for n in app._notifications if "Could not save" in n.message]
        assert len(saves) >= 3

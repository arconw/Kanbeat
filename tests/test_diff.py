import colorsys
import shutil
import subprocess
from pathlib import Path

import pytest

from kanbeat.diff import (
    collect_changes,
    is_generated,
    parse_diff,
    read_untracked,
)
from kanbeat.ui.diff_render import (
    ADD_COLOR,
    REMOVE_COLOR,
    TOKEN_STYLES,
    lexer_for_path,
    render_file,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is required")

CALC = "def add(a, b):\n    return a - b\n\ndef mul(a, b):\n    return a * b\n"


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
    (tmp_path / "calc.py").write_text(CALC)
    (tmp_path / "README.md").write_text("# demo\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "init")
    return tmp_path


def test_parse_modified_file_numbers_and_function_context():
    text = (
        "diff --git a/calc.py b/calc.py\n"
        "index 1fd6c62..d13c48a 100644\n"
        "--- a/calc.py\n"
        "+++ b/calc.py\n"
        "@@ -1,5 +1,8 @@ def add(a, b):\n"
        " def add(a, b):\n"
        "-    return a - b\n"
        "+    return a + b\n"
        " \n"
        " def mul(a, b):\n"
        "     return a * b\n"
        "+\n"
        "+def sub(a, b):\n"
        "+    return a - b\n"
    )
    [file] = parse_diff(text)
    assert (file.path, file.status, file.additions, file.deletions) == (
        "calc.py",
        "M",
        4,
        1,
    )
    [hunk] = file.hunks
    assert hunk.context == "def add(a, b):"
    assert [(line.kind, line.old_number, line.new_number) for line in hunk.lines][
        :4
    ] == [
        (" ", 1, 1),
        ("-", 2, None),
        ("+", None, 2),
        (" ", 3, 3),
    ]


def test_word_marks_cover_only_the_changed_words():
    text = (
        "diff --git a/c.py b/c.py\n--- a/c.py\n+++ b/c.py\n"
        "@@ -1 +1 @@\n-    return a - b\n+    return a + b\n"
    )
    [file] = parse_diff(text)
    removed, added = file.hunks[0].lines
    assert [removed.text[start:end] for start, end in removed.marks] == ["-"]
    assert [added.text[start:end] for start, end in added.marks] == ["+"]


def test_completely_different_lines_get_no_word_marks():
    text = (
        "diff --git a/c.py b/c.py\n--- a/c.py\n+++ b/c.py\n"
        "@@ -1 +1 @@\n-alpha beta gamma\n+one two three\n"
    )
    [file] = parse_diff(text)
    assert [line.marks for line in file.hunks[0].lines] == [[], []]


def test_parse_rename_binary_delete_and_new_file():
    text = (
        "diff --git a/old name.txt b/new name.txt\n"
        "similarity index 100%\n"
        "rename from old name.txt\n"
        "rename to new name.txt\n"
        "diff --git a/img.png b/img.png\n"
        "index 1..2 100644\n"
        "Binary files a/img.png and b/img.png differ\n"
        "diff --git a/gone.txt b/gone.txt\n"
        "deleted file mode 100644\n"
        "--- a/gone.txt\n"
        "+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n-one\n-two\n"
        "diff --git a/fresh.txt b/fresh.txt\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/fresh.txt\n"
        "@@ -0,0 +1 @@\n+hello\n\\ No newline at end of file\n"
    )
    renamed, binary, deleted, fresh = parse_diff(text)
    assert (renamed.status, renamed.old_path, renamed.path) == (
        "R",
        "old name.txt",
        "new name.txt",
    )
    assert binary.binary and binary.path == "img.png"
    assert (deleted.status, deleted.deletions, deleted.additions) == ("D", 2, 0)
    assert (fresh.status, fresh.additions, fresh.hunks[0].lines[0].new_number) == (
        "A",
        1,
        1,
    )


def test_content_lines_that_look_like_headers_stay_content():
    text = (
        "diff --git a/n.txt b/n.txt\n--- a/n.txt\n+++ b/n.txt\n"
        "@@ -1,2 +1,2 @@\n--- not a header\n+++ also not\n same\n"
        "diff --git a/m.txt b/m.txt\n--- a/m.txt\n+++ b/m.txt\n"
        "@@ -1 +1 @@\n-x\n+y\n"
    )
    first, second = parse_diff(text)
    assert [line.text for line in first.hunks[0].lines] == [
        "-- not a header",
        "++ also not",
        "same",
    ]
    assert second.path == "m.txt"


def test_collect_changes_sees_modified_new_and_untracked_files(repo):
    (repo / "calc.py").write_text(
        CALC.replace("a - b", "a + b") + "\ndef sub(a, b):\n    return a - b\n"
    )
    (repo / "notes.txt").write_text("one\ntwo\n")
    changes = collect_changes(repo)
    assert changes.available
    assert [file.path for file in changes.files] == ["calc.py", "notes.txt"]
    calc, notes = changes.files
    assert (calc.status, calc.additions, calc.deletions) == ("M", 4, 1)
    assert (notes.status, notes.additions, notes.deletions) == ("?", 2, 0)
    assert [line.text for line in notes.hunks[0].lines] == ["one", "two"]
    assert (changes.additions, changes.deletions) == (6, 1)


def test_collect_changes_includes_staged_changes_and_reports_unchanged(repo):
    (repo / "README.md").write_text("# demo\nmore\n")
    git(repo, "add", "README.md")
    first = collect_changes(repo)
    assert [file.path for file in first.files] == ["README.md"]
    again = collect_changes(repo, first.signature)
    assert again.unchanged and again.files == []
    (repo / "README.md").write_text("# demo\nmore\neven more\n")
    assert collect_changes(repo, first.signature).files[0].additions == 2


def test_clean_repository_has_no_files(repo):
    changes = collect_changes(repo)
    assert changes.available and changes.files == []


def test_repository_without_commits_shows_everything_added(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_text("a\n")
    git(tmp_path, "add", "a.txt")
    (tmp_path / "b.txt").write_text("b\n")
    changes = collect_changes(tmp_path)
    assert [(file.path, file.status) for file in changes.files] == [
        ("a.txt", "A"),
        ("b.txt", "?"),
    ]


def test_not_a_repository_is_unavailable(tmp_path):
    changes = collect_changes(tmp_path)
    assert not changes.available and changes.files == []


def test_runtime_state_directory_is_never_listed(repo):
    state = repo / ".kanbeat" / ".state"
    state.mkdir(parents=True)
    (state / "supervisor.log").write_text("noise\n")
    (repo / ".kanbeat" / "INSTRUCTIONS.md").write_text("guide\n")
    paths = [file.path for file in collect_changes(repo).files]
    assert paths == [".kanbeat/INSTRUCTIONS.md"]


def test_project_in_a_subdirectory_reports_relative_paths(repo):
    package = repo / "pkg"
    package.mkdir()
    (package / "mod.py").write_text("x = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "pkg")
    (package / "mod.py").write_text("x = 2\n")
    (repo / "calc.py").write_text("changed\n")
    assert [file.path for file in collect_changes(package).files] == ["mod.py"]


def test_untracked_binary_symlink_and_large_files(tmp_path):
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02")
    (tmp_path / "link").symlink_to("blob.bin")
    (tmp_path / "big.txt").write_text("line\n" * 100000)
    assert read_untracked(tmp_path, "blob.bin").binary
    assert read_untracked(tmp_path, "link").note == "Not a regular file"
    big = read_untracked(tmp_path, "big.txt")
    assert big.note.startswith("Large file") and big.shown_lines == 4000


def test_sources_pick_staged_unstaged_and_last_commit(repo):
    (repo / "calc.py").write_text(CALC.replace("a - b", "a + b"))
    git(repo, "add", "calc.py")
    (repo / "README.md").write_text("# demo\nmore\n")
    (repo / "fresh.txt").write_text("x\n")
    paths = lambda source: [f.path for f in collect_changes(repo, "", source).files]
    assert paths("current") == ["README.md", "calc.py", "fresh.txt"]
    assert paths("staged") == ["calc.py"]
    assert paths("unstaged") == ["README.md", "fresh.txt"]
    assert paths("last") == ["README.md", "calc.py"]


def test_last_commit_of_an_empty_repository_is_empty(tmp_path):
    git(tmp_path, "init", "-q")
    changes = collect_changes(tmp_path, "", "last")
    assert changes.available and changes.files == []


def test_signature_depends_on_the_source(repo):
    (repo / "calc.py").write_text("changed\n")
    assert (
        collect_changes(repo, "", "current").signature
        != collect_changes(repo, "", "unstaged").signature
    )


@pytest.mark.parametrize(
    "path,generated",
    [
        ("tests/test_a.py", True),
        ("pkg/test_a.py", True),
        ("src/a_test.py", True),
        ("web/app.test.ts", True),
        ("uv.lock", True),
        ("dist/bundle.js", True),
        ("src/kanbeat/ui/diff.py", False),
        ("README.md", False),
        ("docs/latest.md", False),
    ],
)
def test_tests_and_generated_files_are_recognised(path, generated):
    assert is_generated(path) is generated


def test_render_highlights_python_and_uses_one_number_column(repo):
    (repo / "calc.py").write_text(CALC.replace("a - b", "a + b"))
    [file] = collect_changes(repo).files
    assert lexer_for_path("calc.py") is not None
    assert lexer_for_path("data.unknownext") is None
    rendered = render_file(file)
    assert [(line.kind, line.number) for line in rendered.lines][:3] == [
        (" ", 1),
        ("-", 2),
        ("+", 2),
    ]
    marked = [
        line.content.plain[span.start : span.end]
        for line in rendered.lines
        for span in line.content.spans
        if getattr(span.style, "bgcolor", None) is not None
        and span.style.bgcolor.get_truecolor().hex == "#1d6b36"
    ]
    assert marked == ["+"]
    keywords = [
        line.content.plain[span.start : span.end]
        for line in rendered.lines
        for span in line.content.spans
        if getattr(span.style, "color", None) is not None
        and span.style.color.get_truecolor().hex == "#c4a7ff"
    ]
    assert "def" in keywords and "return" in keywords


def clashes(color):
    red, green, blue = (channel / 255 for channel in color.get_truecolor())
    hue, saturation, value = colorsys.rgb_to_hsv(red, green, blue)
    degrees = hue * 360
    if saturation < 0.35 or value < 0.3:
        return False
    return degrees <= 20 or degrees >= 340 or 80 <= degrees <= 160


def test_syntax_colours_never_look_like_the_red_or_green_highlights():
    clashing = [
        name
        for name, style in TOKEN_STYLES.items()
        if style.color is not None and clashes(style.color)
    ]
    assert clashing == []
    assert ADD_COLOR != REMOVE_COLOR

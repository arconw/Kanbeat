from __future__ import annotations

import asyncio
import difflib
import hashlib
import os
import re
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
EXCLUDE_STATE = ":(exclude).kanbeat/.state"
GIT_TIMEOUT = 15
TAB_WIDTH = 4
UNTRACKED_FILES_LIMIT = 300
UNTRACKED_BYTES_LIMIT = 262144
BINARY_SNIFF_BYTES = 8000
FILE_LINES_LIMIT = 4000
TOTAL_LINES_LIMIT = 40000
MARK_LINE_LIMIT = 1000
MARK_SIMILARITY = 0.5
HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")
WORDS = re.compile(r"\w+|\s+|[^\w\s]")
SOURCES = {
    "current": "Current",
    "unstaged": "Unstaged",
    "staged": "Staged",
    "last": "Last commit",
}
GENERATED_DIRECTORIES = {
    "tests",
    "test",
    "__tests__",
    "spec",
    "dist",
    "build",
    "node_modules",
    "__pycache__",
    "generated",
    "vendor",
}
GENERATED_SUFFIXES = (
    "_test.py",
    ".test.js",
    ".test.ts",
    ".test.tsx",
    ".spec.js",
    ".spec.ts",
    ".snap",
    ".min.js",
    ".min.css",
    ".lock",
)
GENERATED_NAMES = {"package-lock.json", "pnpm-lock.yaml", "go.sum"}


@dataclass
class DiffLine:
    kind: str
    text: str
    old_number: int | None
    new_number: int | None
    marks: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class Hunk:
    old_start: int
    new_start: int
    context: str
    lines: list[DiffLine] = field(default_factory=list)
    old_count: int = 0
    new_count: int = 0


@dataclass
class FileDiff:
    path: str
    status: str = "M"
    old_path: str | None = None
    additions: int = 0
    deletions: int = 0
    binary: bool = False
    note: str = ""
    hunks: list[Hunk] = field(default_factory=list)
    hidden_lines: int = 0
    shown_lines: int = 0


@dataclass
class Changes:
    available: bool = False
    signature: str = ""
    unchanged: bool = False
    files: list[FileDiff] = field(default_factory=list)

    @property
    def additions(self) -> int:
        return sum(file.additions for file in self.files)

    @property
    def deletions(self) -> int:
        return sum(file.deletions for file in self.files)


def is_generated(path: str) -> bool:
    parts = path.lower().split("/")
    name = parts[-1]
    return (
        any(part in GENERATED_DIRECTORIES for part in parts[:-1])
        or name.startswith("test_")
        or name.endswith(GENERATED_SUFFIXES)
        or name in GENERATED_NAMES
    )


def expand_tabs(text: str) -> str:
    return text.expandtabs(TAB_WIDTH)


def header_path(line: str) -> str:
    rest = line[len("diff --git ") :]
    index = rest.rfind(" b/")
    name = rest[index + 3 :] if index != -1 else rest
    return name.strip('"')


def word_offsets(words: list[str]) -> list[int]:
    offsets = [0]
    for word in words:
        offsets.append(offsets[-1] + len(word))
    return offsets


def pair_marks(removed: DiffLine, added: DiffLine) -> None:
    if max(len(removed.text), len(added.text)) > MARK_LINE_LIMIT:
        return
    old_words = WORDS.findall(removed.text)
    new_words = WORDS.findall(added.text)
    matcher = difflib.SequenceMatcher(None, old_words, new_words, autojunk=False)
    if matcher.ratio() < MARK_SIMILARITY:
        return
    old_at = word_offsets(old_words)
    new_at = word_offsets(new_words)
    for tag, old_from, old_to, new_from, new_to in matcher.get_opcodes():
        if tag in ("replace", "delete") and old_to > old_from:
            removed.marks.append((old_at[old_from], old_at[old_to]))
        if tag in ("replace", "insert") and new_to > new_from:
            added.marks.append((new_at[new_from], new_at[new_to]))


def mark_words(lines: list[DiffLine]) -> None:
    index = 0
    while index < len(lines):
        if lines[index].kind != "-":
            index += 1
            continue
        start = index
        while index < len(lines) and lines[index].kind == "-":
            index += 1
        middle = index
        while index < len(lines) and lines[index].kind == "+":
            index += 1
        for removed, added in zip(lines[start:middle], lines[middle:index]):
            pair_marks(removed, added)


def parse_diff(text: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    current: FileDiff | None = None
    hunk: Hunk | None = None
    old_left = new_left = old_number = new_number = 0
    total = 0
    for line in text.split("\n"):
        if current is not None and hunk is not None and (old_left > 0 or new_left > 0):
            marker, body = line[:1], line[1:]
            if marker == "\\":
                continue
            if marker == "+":
                kind, old_at, new_at = "+", None, new_number
                new_left -= 1
                new_number += 1
                current.additions += 1
            elif marker == "-":
                kind, old_at, new_at = "-", old_number, None
                old_left -= 1
                old_number += 1
                current.deletions += 1
            else:
                kind, old_at, new_at = " ", old_number, new_number
                old_left -= 1
                new_left -= 1
                old_number += 1
                new_number += 1
            if current.shown_lines >= FILE_LINES_LIMIT or total >= TOTAL_LINES_LIMIT:
                current.hidden_lines += 1
                continue
            hunk.lines.append(DiffLine(kind, expand_tabs(body), old_at, new_at))
            current.shown_lines += 1
            total += 1
        elif line.startswith("diff --git "):
            current = FileDiff(path=header_path(line))
            files.append(current)
            hunk = None
        elif current is None:
            continue
        elif line.startswith("@@"):
            match = HUNK_HEADER.match(line)
            if match is None:
                continue
            old_start, old_count, new_start, new_count, context = match.groups()
            hunk = Hunk(
                old_start=int(old_start),
                new_start=int(new_start),
                context=context.strip(),
                old_count=int(old_count) if old_count is not None else 1,
                new_count=int(new_count) if new_count is not None else 1,
            )
            current.hunks.append(hunk)
            old_left, new_left = hunk.old_count, hunk.new_count
            old_number, new_number = hunk.old_start, hunk.new_start
        elif hunk is None:
            if line.startswith("new file mode"):
                current.status = "A"
            elif line.startswith("deleted file mode"):
                current.status = "D"
            elif line.startswith("rename from "):
                current.status = "R"
                current.old_path = line[len("rename from ") :]
            elif line.startswith("rename to "):
                current.path = line[len("rename to ") :]
            elif line.startswith("Binary files") or line.startswith("GIT binary patch"):
                current.binary = True
            elif line.startswith("+++ b/"):
                current.path = line[len("+++ b/") :].split("\t")[0]
    for file in files:
        for item in file.hunks:
            mark_words(item.lines)
    return files


def read_untracked(project: Path, relative: str) -> FileDiff:
    file = FileDiff(path=relative, status="?")
    path = project / relative
    try:
        info = path.lstat()
    except OSError:
        file.note = "File is not readable"
        return file
    if not stat.S_ISREG(info.st_mode):
        file.note = "Not a regular file"
        return file
    try:
        with path.open("rb") as handle:
            data = handle.read(UNTRACKED_BYTES_LIMIT + 1)
    except OSError:
        file.note = "File is not readable"
        return file
    if b"\0" in data[:BINARY_SNIFF_BYTES]:
        file.binary = True
        return file
    if len(data) > UNTRACKED_BYTES_LIMIT:
        data = data[:UNTRACKED_BYTES_LIMIT]
        file.note = "Large file, showing only the beginning"
    lines = data.decode("utf-8", "replace").splitlines()
    if not lines:
        return file
    shown = lines[:FILE_LINES_LIMIT]
    file.hidden_lines = len(lines) - len(shown)
    file.shown_lines = len(shown)
    file.additions = len(lines)
    file.hunks.append(
        Hunk(
            old_start=0,
            new_start=1,
            context="",
            old_count=0,
            new_count=len(shown),
            lines=[
                DiffLine("+", expand_tabs(text), None, number)
                for number, text in enumerate(shown, 1)
            ],
        )
    )
    return file


def git(project: Path, *args: str) -> bytes | None:
    try:
        result = subprocess.run(
            ["git", "-c", "core.quotepath=off", *args],
            cwd=project,
            capture_output=True,
            timeout=GIT_TIMEOUT,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def split_names(output: bytes | None) -> list[str]:
    if not output:
        return []
    return [name for name in output.decode("utf-8", "replace").split("\0") if name]


def untracked_names(project: Path) -> list[str]:
    return split_names(
        git(
            project,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            ".",
            EXCLUDE_STATE,
        )
    )


def untracked_signature(project: Path, names: list[str]) -> bytes:
    parts = [str(len(names)).encode()]
    for name in names[:UNTRACKED_FILES_LIMIT]:
        try:
            info = (project / name).lstat()
        except OSError:
            continue
        parts.append(f"{name}\0{info.st_size}\0{info.st_mtime_ns}".encode())
    return b"\n".join(parts)


def source_arguments(project: Path, source: str) -> tuple[list[str], bool] | None:
    head = git(project, "rev-parse", "--verify", "-q", "HEAD")
    base = head.decode().strip() if head else EMPTY_TREE
    if source == "unstaged":
        return ["diff"], True
    if source == "staged":
        return ["diff", "--cached"], False
    if source == "last":
        if not head:
            return None
        parent = git(project, "rev-parse", "--verify", "-q", "HEAD~1")
        return ["diff", parent.decode().strip() if parent else EMPTY_TREE, base], False
    return ["diff", base], True


def collect_changes(
    project: Path, previous: str = "", source: str = "current"
) -> Changes:
    inside = git(project, "rev-parse", "--is-inside-work-tree")
    if inside is None or inside.strip() != b"true":
        return Changes(signature="unavailable")
    arguments = source_arguments(project, source)
    if arguments is None:
        return Changes(available=True, signature="empty:" + source)
    command, with_untracked = arguments
    raw = git(
        project,
        *command,
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "-M",
        "--relative",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        "--",
        ".",
        EXCLUDE_STATE,
    )
    if raw is None:
        return Changes(signature="unavailable")
    names = untracked_names(project) if with_untracked else []
    digest = hashlib.sha1(source.encode() + b"\0" + raw)
    digest.update(untracked_signature(project, names))
    signature = digest.hexdigest()
    if signature == previous:
        return Changes(available=True, signature=signature, unchanged=True)
    files = parse_diff(raw.decode("utf-8", "replace"))
    for name in names[:UNTRACKED_FILES_LIMIT]:
        files.append(read_untracked(project, name))
    for name in names[UNTRACKED_FILES_LIMIT:]:
        files.append(
            FileDiff(path=name, status="?", note="Not shown, too many new files")
        )
    files.sort(key=lambda file: file.path)
    return Changes(available=True, signature=signature, files=files)


async def collect(
    project: Path, previous: str = "", source: str = "current"
) -> Changes:
    return await asyncio.to_thread(collect_changes, project, previous, source)

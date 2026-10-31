"""Filesystem-native tickets. No YAML frontmatter, no mutable ticket metadata."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import json
import fcntl
import hashlib
from contextlib import contextmanager
import os
import tempfile
from typing import Any

COLUMNS = ("backlog", "todo", "progress", "review", "done", "failed", "archive")
ACTIVE = ("progress", "review")
FILENAME = re.compile(
    r"^(?P<id>\d{3,})-(?P<slug>(?!\d+-)[a-z0-9]+(?:-[a-z0-9]+)*)\.md$"
)
SLUG = re.compile(r"[^a-z0-9]+")
NUMBERED_SLUG = re.compile(r"\d+-")

INSTRUCTION = """# Kanbeat ticket-writing guide

This file is **not** loaded automatically as an agent instruction.
Read it only when explicitly asked to create or modify Kanbeat tickets.

- Create tickets in `.kanbeat/backlog/` by default.
- Ticket filenames: `<unique-id>-<slug>.md`.
- The number is a stable unique ID across board **and archive**.
- The slug is descriptive. Preserve the ID when renaming or moving a ticket.
- Ticket content is plain Markdown: one `# Title`, then task description and
  optional acceptance criteria. No frontmatter or internal runtime metadata.
- Use unused IDs. Never modify `.state/` or files belonging to another active
  ticket, and change settings with `kanbeat config`, not by editing
  `config.json`. Do not write completion/status markers to tickets.
- To queue work, move the file to `todo/`.
- Existing ticket content is user-owned. Do not insert harness transcripts.

## Dependencies between tickets

Kanbeat runs independent tickets **in parallel** (up to `max_parallel` in
`config.json`). Tickets that have no dependencies are all started at the same
time, so declare a dependency whenever one ticket needs the result of another
(same file or module, an API another ticket introduces, required sequence).

- Declare it inside the ticket that has to wait, on its own line right after
  the `# Title`, as a list of ticket IDs:

      Blocked by: #002, #003

- The IDs are the unique IDs from the filenames. A ticket in `todo/` is started
  only when every blocker is in `done/` or `archive/`; until then it stays in
  `todo/` and starts by itself later. Queue a whole batch in `todo/` and it
  works through itself in dependency order.
- Write dependencies only in the blocked ticket. Never write a "blocks" line:
  Kanbeat derives it and shows it in the TUI.
- A chain (`003` blocked by `002`, `002` blocked by `001`) is picked one ticket
  at a time, from the first. Never create cycles.
- Prefer few, real dependencies. Unneeded ones remove parallelism.

## Managing the board

A ticket's status is the folder it sits in, so you can run the board with plain
files and the `kanbeat` command. Run the command from the project directory.

- See the board: `kanbeat status` (add `--json` for a machine-readable form) or
  `kanbeat list`.
- Queue work by moving a ticket from `backlog/` to `todo/` (`mv`, or
  `kanbeat move 004 todo`). While the board is running Kanbeat moves it on
  through `progress/`, `review/` and `done/` itself. Do not move a ticket that
  is in `progress/` or `review/`, and do not create `done/` entries by hand.
- `kanbeat play` starts the board and `kanbeat stop` stops it.
- Never edit `.kanbeat/.state/`. It is the supervisor's private runtime state,
  kept in memory and rewritten from there, so a hand edit is lost or corrupts
  it. Status never lives there; it is the folder.
- Settings: `kanbeat config get [key]`, `kanbeat config set <key> <value>`
  (`--file PATH` or `-` for a long prompt), `kanbeat config history` and
  `kanbeat config revert <id>`. The keys are `default_harness`, `max_parallel`,
  `global_cp`, `developer_prompt`, `reviewer_prompt`, `transition_hook.harness`
  and `transition_hook.prompt`. A change takes effect at once, shows as a
  notice in the TUI and can be undone from Settings or with `config revert`.
  Change only what you were asked to change.
- To follow ticket changes, run `kanbeat watch --json` as a background process:
  it prints one line per event (`created`, `moved`, `deleted`, `execution`)
  until you stop it.
"""

LEGACY_INSTRUCTION_HASHES = frozenset(
    {
        "91b1a7999b9b6d3ee5f282ba786da785d85e4fc8876d39bfc493f78aed3626c1",
        "cca0e0df29cef497c96fce72141ccba86438f8c902f8d433b74383d510167584",
        "a101523d49b881ab841947b74e4de997bb98d93e1978acd7aafb44a1061713a2",
    }
)


@dataclass(frozen=True)
class Ticket:
    id: str
    slug: str
    column: str
    path: Path
    title: str
    body: str


FENCE = re.compile(r"^\s*(```|~~~)")
BLOCKED_LINE = re.compile(
    r"^\s*(?:[-*]\s+)?(?:\*\*|__)?blocked[ _-]?by(?:\*\*|__)?\s*:(?:\*\*|__)?(?P<ids>.*)$",
    re.IGNORECASE,
)
BLOCKED_HEADING = re.compile(r"^#{2,6}\s*blocked[ _-]?by\s*$", re.IGNORECASE)
TICKET_REF = re.compile(r"(?<![\w.])#?(\d+)(?![\w.])")
INLINE_REF = re.compile(r"(?<![\w.])#(\d+)(?!\w)(?!\.\d)")


def ticket_ref(text: str) -> str:
    return str(int(text)).zfill(3)


def blocker_ids(body: str) -> list[str]:
    found: list[str] = []
    in_fence = False
    in_section = False
    for line in body.splitlines():
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        line_match = BLOCKED_LINE.match(line)
        refs: list[str] = []
        if line_match:
            in_section = False
            refs = INLINE_REF.findall(line_match["ids"])
        elif BLOCKED_HEADING.match(line):
            in_section = True
        elif in_section:
            if line.lstrip().startswith("#"):
                in_section = False
            elif line.strip():
                refs = TICKET_REF.findall(
                    line.split(":", 1)[0] if ":" in line else line
                )[:1]
        for ref in refs:
            ref = ticket_ref(ref)
            if ref not in found:
                found.append(ref)
    return found


def is_blocker_line(line: str) -> bool:
    return bool(BLOCKED_LINE.match(line) or BLOCKED_HEADING.match(line))


SATISFYING = ("done", "archive")


def dependencies(tickets: list[Ticket]) -> dict[str, dict[str, Any]]:
    by_id = {ticket.id: ticket for ticket in tickets}
    result: dict[str, dict[str, Any]] = {
        ticket.id: {"blocked_by": [], "blocks": [], "blocked": False}
        for ticket in tickets
    }
    for ticket in tickets:
        for blocker_id in blocker_ids(ticket.body):
            if blocker_id == ticket.id:
                continue
            blocker = by_id.get(blocker_id)
            satisfied = blocker is not None and blocker.column in SATISFYING
            result[ticket.id]["blocked_by"].append(
                {
                    "id": blocker_id,
                    "title": blocker.title if blocker else "",
                    "column": blocker.column if blocker else "missing",
                    "satisfied": satisfied,
                }
            )
            if not satisfied:
                result[ticket.id]["blocked"] = True
            if blocker is not None:
                result[blocker_id]["blocks"].append(
                    {
                        "id": ticket.id,
                        "title": ticket.title,
                        "column": ticket.column,
                        "satisfied": satisfied,
                    }
                )
    return result


def dependency_problems(
    waiting: list[Ticket], tickets: list[Ticket]
) -> dict[str, list[str]]:
    links = dependencies(tickets)
    by_id = {ticket.id: ticket for ticket in tickets}
    waiting_ids = {ticket.id for ticket in waiting}

    def reaches(start: str, goal: str, seen: set[str]) -> bool:
        for row in links[start]["blocked_by"]:
            target = row["id"]
            if target == goal:
                return True
            if target in waiting_ids and target not in seen:
                seen.add(target)
                if reaches(target, goal, seen):
                    return True
        return False

    problems: dict[str, list[str]] = {}
    for ticket in waiting:
        reasons = []
        for row in links[ticket.id]["blocked_by"]:
            if row["satisfied"]:
                continue
            blocker = by_id.get(row["id"])
            if blocker is None:
                reasons.append(f"#{row['id']} is missing")
            elif blocker.column in ("backlog", "failed"):
                reasons.append(f"#{row['id']} is in {blocker.column}")
        if reaches(ticket.id, ticket.id, set()):
            reasons.append("dependency cycle")
        if reasons:
            problems[ticket.id] = reasons
    return problems


def unblocked(candidates: list[Ticket], tickets: list[Ticket]) -> list[Ticket]:
    links = dependencies(tickets)
    return [ticket for ticket in candidates if not links[ticket.id]["blocked"]]


class Board:
    def __init__(self, project: Path | str):
        self.project = Path(project).expanduser().resolve()
        self.root = self.project / ".kanbeat"

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for column in COLUMNS:
            (self.root / column).mkdir(exist_ok=True)
        instructions = self.root / "INSTRUCTIONS.md"
        if (
            not instructions.exists()
            or hashlib.sha256(instructions.read_bytes()).hexdigest()
            in LEGACY_INSTRUCTION_HASHES
        ):
            instructions.write_text(INSTRUCTION, encoding="utf-8")

    def scan(self) -> tuple[list[Ticket], list[str]]:
        tickets: list[Ticket] = []
        errors: list[str] = []
        ids: set[str] = set()
        for column in COLUMNS:
            directory = self.root / column
            if not directory.is_dir():
                continue
            for path in directory.iterdir():
                if not path.is_file() or path.suffix != ".md":
                    continue
                match = FILENAME.fullmatch(path.name)
                if not match:
                    errors.append(f"Invalid ticket filename: {column}/{path.name}")
                    continue
                ticket_id = match["id"]
                if ticket_id in ids:
                    errors.append(
                        f"Duplicate ticket ID {ticket_id}: {column}/{path.name}"
                    )
                    continue
                ids.add(ticket_id)
                try:
                    content = path.read_text(encoding="utf-8")
                except (OSError, UnicodeError) as exc:
                    errors.append(f"Unreadable ticket {path.name}: {exc}")
                    continue
                title = next(
                    (
                        line[2:].strip()
                        for line in content.splitlines()
                        if line.startswith("# ") and line[2:].strip()
                    ),
                    match["slug"].replace("-", " ").title(),
                )
                tickets.append(
                    Ticket(
                        id=ticket_id,
                        slug=match["slug"],
                        column=column,
                        path=path,
                        title=title,
                        body=content,
                    )
                )
        tickets.sort(key=lambda t: (COLUMNS.index(t.column), int(t.id)))
        return tickets, errors

    def find(self, ticket_id: str) -> Ticket:
        if not ticket_id.isdecimal():
            raise ValueError("Ticket ID must be numeric")
        tickets, _ = self.scan()
        matches = [t for t in tickets if t.id == ticket_id]
        if len(matches) != 1:
            raise ValueError(f"Ticket ID not uniquely found: {ticket_id}")
        return matches[0]

    @contextmanager
    def id_lock(self):
        path = self.root / ".state" / "ids.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            yield

    def remember_ids(self, tickets: list[Ticket]) -> int:
        path = self.root / "ids.json"
        previous = 0
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(data, dict)
                or type(data.get("last_id")) is not int
                or data["last_id"] < 0
            ):
                raise ValueError("ids.json must contain a nonnegative integer last_id")
            previous = data["last_id"]
        number = max(previous, max((int(t.id) for t in tickets), default=0))
        atomic_json(path, {"last_id": number})
        return number

    def _next_id(self, tickets: list[Ticket]) -> str:
        number = self.remember_ids(tickets) + 1
        atomic_json(self.root / "ids.json", {"last_id": number})
        return str(number).zfill(3)

    def create(self, title: str, body: str = "", column: str = "backlog") -> Ticket:
        if column not in COLUMNS or column == "archive":
            raise ValueError("New tickets must be placed on the board")
        with self.id_lock():
            tickets, errors = self.scan()
            if errors:
                raise ValueError(
                    "Fix invalid tickets before creating more: " + errors[0]
                )
            ticket_id = self._next_id(tickets)
            slug = SLUG.sub("-", title.lower()).strip("-") or "ticket"
            slug = slug[:72].rstrip("-")
            if NUMBERED_SLUG.match(slug):
                slug = "ticket-" + slug
            path = self.root / column / f"{ticket_id}-{slug}.md"
            text = "# " + title.strip() + "\n\n" + body.strip() + "\n"
            # exclusive create: do not overwrite a file created externally
            with path.open("x", encoding="utf-8") as stream:
                stream.write(text)
            return self.find(ticket_id)

    def move(self, ticket_id: str, column: str) -> Ticket:
        if column not in COLUMNS:
            raise ValueError("Invalid column")
        ticket = self.find(ticket_id)
        if ticket.column == column:
            return ticket
        target = self.root / column / ticket.path.name
        if target.exists():
            raise FileExistsError(target)
        ticket.path.rename(target)
        return self.find(ticket_id)

    def remove_archived(self, ticket_id: str) -> None:
        with self.id_lock():
            ticket = self.find(ticket_id)
            if ticket.column != "archive":
                raise ValueError("Only archived tickets can be deleted")
            tickets, errors = self.scan()
            if errors:
                raise ValueError("Fix invalid tickets before deleting: " + errors[0])
            self.remember_ids(tickets)
            ticket.path.unlink()


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    temp: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".writing-",
            delete=False,
        ) as stream:
            temp = stream.name
            os.chmod(temp, 0o600)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp and Path(temp).exists():
            Path(temp).unlink()

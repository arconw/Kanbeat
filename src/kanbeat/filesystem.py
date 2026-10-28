"""Filesystem-native tickets. No YAML frontmatter, no mutable ticket metadata."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import os
import tempfile
from typing import Any

COLUMNS = ("backlog", "todo", "progress", "review", "done", "failed", "archive")
ACTIVE = ("progress", "review")
FILENAME = re.compile(
    r"^(?P<order>\d+)-(?P<id>\d{3,})-(?P<slug>[a-z0-9]+(?:-[a-z0-9]+)*)\.md$"
)
SLUG = re.compile(r"[^a-z0-9]+")

INSTRUCTION = """# Kanbeat ticket-writing guide

This file is **not** loaded automatically as an agent instruction.
Read it only when explicitly asked to create or modify Kanbeat tickets.

- Create tickets in `.kanbeat/backlog/` by default.
- Ticket filenames: `<order>-<unique-id>-<slug>.md`.
- The first number is the numeric ordering key **within its column**.
- The second number is a stable unique ID across board **and archive**.
- The slug is descriptive. Preserve the ID when renaming or moving a ticket.
- Ticket content is plain Markdown: one `# Title`, then task description and
  optional acceptance criteria. No frontmatter or internal runtime metadata.
- Use unused IDs. Never modify `.state/`, `config.json` or files belonging
  to another active ticket. Do not write completion/status markers to tickets.
- To queue work, move the file to `todo/` and choose the numeric order.
- Existing ticket content is user-owned. Do not insert harness transcripts.
"""


@dataclass(frozen=True)
class Ticket:
    id: str
    order: int
    slug: str
    column: str
    path: Path
    title: str
    body: str


class Board:
    def __init__(self, project: Path | str):
        self.project = Path(project).expanduser().resolve()
        self.root = self.project / ".kanbeat"

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for column in COLUMNS:
            (self.root / column).mkdir(exist_ok=True)
        instructions = self.root / "INSTRUCTIONS.md"
        if not instructions.exists():
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
                        order=int(match["order"]),
                        slug=match["slug"],
                        column=column,
                        path=path,
                        title=title,
                        body=content,
                    )
                )
        tickets.sort(key=lambda t: (COLUMNS.index(t.column), t.order, int(t.id)))
        return tickets, errors

    def find(self, ticket_id: str) -> Ticket:
        if not ticket_id.isdecimal():
            raise ValueError("Ticket ID must be numeric")
        tickets, _ = self.scan()
        matches = [t for t in tickets if t.id == ticket_id]
        if len(matches) != 1:
            raise ValueError(f"Ticket ID not uniquely found: {ticket_id}")
        return matches[0]

    def _next_id(self, tickets: list[Ticket]) -> str:
        number = max((int(t.id) for t in tickets), default=0) + 1
        return str(number).zfill(3)

    def create(self, title: str, body: str = "", column: str = "backlog") -> Ticket:
        if column not in COLUMNS or column == "archive":
            raise ValueError("New tickets must be placed on the board")
        tickets, errors = self.scan()
        if errors:
            raise ValueError("Fix invalid tickets before creating more: " + errors[0])
        ticket_id = self._next_id(tickets)
        order = max((t.order for t in tickets if t.column == column), default=-10) + 10
        slug = SLUG.sub("-", title.lower()).strip("-") or "ticket"
        slug = slug[:72].rstrip("-")
        path = self.root / column / f"{order}-{ticket_id}-{slug}.md"
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
        tickets, _ = self.scan()
        order = max((t.order for t in tickets if t.column == column), default=-10) + 10
        target = self.root / column / f"{order}-{ticket.id}-{ticket.slug}.md"
        if target.exists():
            raise FileExistsError(target)
        ticket.path.rename(target)
        return self.find(ticket_id)

    def reorder(self, ticket_id: str, direction: int) -> None:
        if direction not in (-1, 1):
            raise ValueError("Direction must be -1 or 1")
        ticket = self.find(ticket_id)
        tickets, _ = self.scan()
        neighbors = sorted(
            (t for t in tickets if t.column == ticket.column),
            key=lambda t: (t.order, int(t.id)),
        )
        index = next(i for i, t in enumerate(neighbors) if t.id == ticket_id)
        target = index + direction
        if not 0 <= target < len(neighbors):
            return
        neighbors[index], neighbors[target] = neighbors[target], neighbors[index]
        # Two-phase renaming avoids conflicts and preserves file bodies.
        renames: list[tuple[Path, Path]] = []
        for rank, item in enumerate(neighbors):
            temporary = item.path.with_name(f".kanbeat-rename-{item.id}")
            item.path.rename(temporary)
            renames.append(
                (
                    temporary,
                    self.root / item.column / f"{rank * 10}-{item.id}-{item.slug}.md",
                )
            )
        for temporary, destination in renames:
            temporary.rename(destination)

    def remove_archived(self, ticket_id: str) -> None:
        ticket = self.find(ticket_id)
        if ticket.column != "archive":
            raise ValueError("Only archived tickets can be deleted")
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

"""Atomic, human-editable Markdown tickets and project-local settings.

Ticket files are the source of truth; runtime logs and session registry are not.
Mutations use one project-wide POSIX advisory lock and atomic file replacement.
External editors should use atomic renames or the taskbeat CLI for concurrent writes.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
import fcntl
import json
import os
import re
import tempfile
import uuid
from typing import Any, Callable, Iterator

import yaml

STATES = ("backlog", "todo", "progress", "review", "done", "failed")
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)(.*)", re.S)

DEFAULT_CONFIG: dict[str, Any] = {
    "default_harness": "codex",
    "global_cp": "",
    "developer_prompt": "Implement the task. You may write tests, but do not run tests.",
    "reviewer_prompt": "Review the implementation, run appropriate tests, prefer headless tests when feasible.",
    "harness_commands": {
        "codex": ["codex"],
        "claude": ["claude"],
        "opencode": ["opencode"],
        "pi": ["pi"],
    },
    "transition_hook": {"harness": "", "prompt": ""},
}


@dataclass
class Ticket:
    path: Path
    meta: dict[str, Any] = field(default_factory=dict)
    body: str = ""

    @property
    def id(self) -> str:
        return str(self.meta.get("id") or self.path.stem)

    @property
    def title(self) -> str:
        return str(self.meta.get("title") or self.path.stem.replace("-", " ").title())

    @property
    def status(self) -> str:
        state = str(self.meta.get("status") or "backlog").lower().strip()
        return state if state in STATES else "backlog"

    @property
    def order(self) -> int:
        try:
            return int(self.meta.get("order", 100))
        except (TypeError, ValueError):
            return 100

    @property
    def developer(self) -> dict[str, Any]:
        value = self.meta.get("developer")
        return value if isinstance(value, dict) else {}

    @property
    def reviewers(self) -> list[dict[str, Any]]:
        value = self.meta.get("reviewers")
        return [r for r in value if isinstance(r, dict)] if isinstance(value, list) else []

    @property
    def archived(self) -> bool:
        return self.path.parent.name == "archive"


def compact_ticket_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """Omit unset optional role fields from Markdown frontmatter.

    Only TaskBeat-owned fields are compacted. Unknown extension fields are
    preserved, even when empty, to avoid altering external tools' data.
    """
    compacted = dict(meta)

    def compact_role(role: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value for key, value in role.items()
            if not (
                key in ("cp", "session", "harness")
                and (value is None or (isinstance(value, str) and not value.strip()))
            )
        }

    if isinstance(compacted.get("developer"), dict):
        developer = compact_role(compacted["developer"])
        if developer:
            compacted["developer"] = developer
        else:
            compacted.pop("developer")

    if isinstance(compacted.get("reviewers"), list):
        reviewers = [
            compact_role(reviewer) if isinstance(reviewer, dict) else reviewer
            for reviewer in compacted["reviewers"]
        ]
        if reviewers:
            compacted["reviewers"] = reviewers
        else:
            compacted.pop("reviewers")

    if compacted.get("review_iteration") is None:
        compacted.pop("review_iteration", None)

    return compacted


def atomic_write(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".taskbeat-",
            delete=False,
        ) as stream:
            temp_name = stream.name
            os.chmod(temp_name, 0o600)
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


class TicketStore:
    def __init__(self, project: Path | str):
        self.project = Path(project).resolve()
        self.directory = self.project / ".tasks"
        self.archive_dir = self.directory / "archive"
        self.state_dir = self.directory / ".state"
        for path in (self.directory, self.archive_dir, self.state_dir):
            path.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def locked(self) -> Iterator[None]:
        path = self.state_dir / "taskbeat.lock"
        with path.open("a+b") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def validate_id(ticket_id: str) -> str:
        if not ID_PATTERN.fullmatch(ticket_id):
            raise ValueError("Unsafe ticket ID: " + repr(ticket_id))
        return ticket_id

    def path_for(self, ticket_id: str, archived: bool = False) -> Path:
        safe_id = self.validate_id(ticket_id)
        return (self.archive_dir if archived else self.directory) / (safe_id + ".md")

    def read(self, ticket_id: str, archived: bool = False) -> Ticket:
        path = self.path_for(ticket_id, archived)
        text = path.read_text(encoding="utf-8")
        match = FRONTMATTER.match(text)
        if match:
            decoded = yaml.safe_load(match.group(1)) or {}
            if not isinstance(decoded, dict):
                raise ValueError("Frontmatter must be a mapping: " + str(path))
            meta = dict(decoded)
            body = match.group(2)
        else:
            meta = {}
            body = text
            heading = next(
                (line.lstrip("# ").strip() for line in text.splitlines()
                 if line.startswith("# ")), None
            )
            if heading:
                meta["title"] = heading
        if str(meta.get("id", path.stem)) != path.stem:
            raise ValueError("Ticket ID must match file name: " + str(path))
        meta.setdefault("id", path.stem)
        meta.setdefault("status", "backlog")
        meta.setdefault("order", 100)
        return Ticket(path, meta, body)

    def list(self, archived: bool = False) -> list[Ticket]:
        directory = self.archive_dir if archived else self.directory
        tickets: list[Ticket] = []
        for path in directory.glob("*.md"):
            if path.name.startswith("."):
                continue
            try:
                tickets.append(self.read(path.stem, archived))
            except (OSError, ValueError, yaml.YAMLError):
                # A half-written/invalid external ticket must not crash the TUI.
                continue
        return sorted(tickets, key=lambda task: (task.order, task.id))

    def _write(self, ticket: Ticket) -> None:
        ticket.meta = compact_ticket_meta(ticket.meta)
        data = yaml.safe_dump(ticket.meta, allow_unicode=True, sort_keys=False).rstrip()
        atomic_write(ticket.path, "---\n" + data + "\n---\n" + ticket.body.rstrip() + "\n")

    def save(self, ticket: Ticket) -> None:
        self.validate_id(ticket.id)
        if ticket.status not in STATES:
            raise ValueError("Unknown status")
        with self.locked():
            self._write(ticket)

    def create(self, title: str, body: str = "", status: str = "backlog") -> Ticket:
        if status not in STATES:
            raise ValueError("Unknown status: " + status)
        ticket_id = "task-" + uuid.uuid4().hex[:10]
        ticket = Ticket(
            self.path_for(ticket_id),
            {"id": ticket_id, "title": title.strip() or ticket_id,
             "status": status, "order": self.next_order(status),
             },
            body,
        )
        with self.locked():
            self._write(ticket)
        return ticket

    def next_order(self, status: str) -> int:
        orders = [ticket.order for ticket in self.list() if ticket.status == status]
        return (max(orders) if orders else 0) + 100

    def change_status(self, ticket_id: str, desired: str, manual: bool = False) -> tuple[Ticket, str | None]:
        if desired not in STATES:
            raise ValueError("Unknown status: " + desired)
        with self.locked():
            ticket = self.read(ticket_id)
            old = ticket.status
            correction = None
            if not manual and old == "progress" and desired == "done":
                desired = "review"
                correction = "Direct Progress → Done is not allowed. Use Progress → Review → Done."
            if not manual and old == "review" and desired in ("done", "progress") and ticket.reviewers:
                raise ValueError("Reviewers must report using 'taskbeat review' so all votes count.")
            if old == "review" and desired != "review":
                ticket.meta.pop("review_iteration", None)
            ticket.meta["status"] = desired
            if old != desired:
                ticket.meta["order"] = self._next_order_unlocked(desired, ticket.id)
            self._write(ticket)
            return ticket, correction

    def _next_order_unlocked(self, status: str, exclude_id: str) -> int:
        values = [t.order for t in self.list() if t.status == status and t.id != exclude_id]
        return (max(values) if values else 0) + 100

    def reorder(self, ticket_id: str, direction: int) -> None:
        with self.locked():
            ticket = self.read(ticket_id)
            rows = [t for t in self.list() if t.status == ticket.status]
            index = next(i for i, t in enumerate(rows) if t.id == ticket_id)
            other = index + direction
            if 0 <= other < len(rows):
                rows[index], rows[other] = rows[other], rows[index]
                for i, row in enumerate(rows, 1):
                    row.meta["order"] = i * 100
                    self._write(row)

    def begin_review(self, ticket_id: str) -> Ticket:
        with self.locked():
            ticket = self.read(ticket_id)
            if ticket.status != "review":
                raise ValueError("Ticket is not in Review")
            if ticket.meta.get("review_iteration"):
                return ticket
            reviewers = [str(r.get("id")) for r in ticket.reviewers if r.get("id")]
            if len(set(reviewers)) != len(reviewers):
                raise ValueError("Reviewer IDs must be distinct")
            if reviewers:
                ticket.meta["review_iteration"] = {
                    "id": uuid.uuid4().hex[:12], "pending": reviewers, "results": {},
                }
            else:
                ticket.meta["status"] = "done"
            self._write(ticket)
            return ticket

    def report_review(
        self, ticket_id: str, reviewer_id: str, verdict: str, iteration: str | None = None,
    ) -> Ticket:
        if verdict not in ("approve", "changes_requested"):
            raise ValueError("Verdict: approve or changes_requested")
        with self.locked():
            ticket = self.read(ticket_id)
            state = ticket.meta.get("review_iteration")
            if ticket.status != "review" or not isinstance(state, dict):
                raise ValueError("No active review iteration")
            if iteration and iteration != state.get("id"):
                raise ValueError("Stale review iteration; read current ticket again")
            pending = state.get("pending", [])
            if reviewer_id not in pending:
                raise ValueError("Reviewer is not in this iteration")
            outcomes = state.setdefault("results", {})
            if reviewer_id in outcomes and outcomes[reviewer_id] != verdict:
                raise ValueError("Reviewer has already submitted a different verdict")
            outcomes[reviewer_id] = verdict
            if all(member in outcomes for member in pending):
                ticket.meta["status"] = (
                    "progress" if "changes_requested" in outcomes.values() else "done"
                )
                ticket.meta["order"] = self._next_order_unlocked(ticket.status, ticket.id)
                ticket.meta.pop("review_iteration", None)
            self._write(ticket)
            return ticket

    def archive(self, ticket_id: str) -> None:
        with self.locked():
            ticket = self.read(ticket_id)
            ticket.meta["previous_status"] = ticket.status
            ticket.meta["status"] = "backlog"
            ticket.meta["archived_at"] = __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ).isoformat()
            ticket.path = self.path_for(ticket_id, archived=True)
            self._write(ticket)
            self.path_for(ticket_id).unlink()

    def restore(self, ticket_id: str, status: str = "backlog") -> None:
        if status not in ("backlog", "todo"):
            raise ValueError("Restore target must be backlog or todo")
        with self.locked():
            ticket = self.read(ticket_id, archived=True)
            ticket.meta["status"] = status
            ticket.meta["order"] = self._next_order_unlocked(status, ticket.id)
            ticket.meta.pop("archived_at", None)
            ticket.path = self.path_for(ticket_id)
            self._write(ticket)
            self.path_for(ticket_id, archived=True).unlink()

    def delete_archived(self, ticket_id: str) -> None:
        with self.locked():
            self.path_for(ticket_id, archived=True).unlink()

    def config(self) -> dict[str, Any]:
        path = self.directory / "config.json"
        user = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(user, dict):
            raise ValueError("config.json must contain an object")
        config = dict(DEFAULT_CONFIG)
        config.update(user)
        config["harness_commands"] = {
            **DEFAULT_CONFIG["harness_commands"],
            **(user.get("harness_commands", {}) or {}),
        }
        return config

    def save_config(self, config: dict[str, Any]) -> None:
        with self.locked():
            atomic_write(
                self.directory / "config.json",
                json.dumps(config, indent=2, ensure_ascii=False) + "\n",
            )

    def update(self, ticket_id: str, mutate: Callable[[Ticket], None]) -> Ticket:
        with self.locked():
            ticket = self.read(ticket_id)
            mutate(ticket)
            if ticket.status not in STATES:
                raise ValueError("Invalid status")
            self._write(ticket)
            return ticket

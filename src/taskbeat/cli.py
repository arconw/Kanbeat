"""User and agent-facing CLI. State-changing commands are atomic."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from .storage import STATES, TicketStore


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="taskbeat")
    parser.add_argument("--project", type=Path, default=Path.cwd(), help="Project root")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="Open interactive TUI")
    sub.add_parser("list", help="List active tickets")
    create = sub.add_parser("new", help="Create a Markdown ticket")
    create.add_argument("title")
    create.add_argument("--status", choices=STATES, default="backlog")
    create.add_argument("--description", default="")
    status = sub.add_parser("status", help="Update ticket status")
    status.add_argument("ticket")
    status.add_argument("state", choices=STATES)
    review = sub.add_parser("review", help="Submit an independent reviewer verdict")
    review.add_argument("ticket")
    review.add_argument("reviewer")
    review.add_argument("verdict", choices=("approve", "changes_requested"))
    review.add_argument("--iteration", help="Reject stale reviewer sessions")
    return parser


def main() -> None:
    args = make_parser().parse_args()
    store = TicketStore(args.project)
    try:
        if args.command in (None, "run"):
            from .ui.app import TaskBeatApp
            TaskBeatApp(store).run()
        elif args.command == "list":
            for task in store.list():
                print(f"{task.status:10} {task.order:6} {task.id:18} {task.title}")
        elif args.command == "new":
            task = store.create(args.title, args.description, args.status)
            print(task.path)
        elif args.command == "status":
            task, note = store.change_status(args.ticket, args.state)
            print(f"{task.id}: {task.status}")
            if note:
                print(note, file=sys.stderr)
        elif args.command == "review":
            task = store.report_review(
                args.ticket, args.reviewer, args.verdict, args.iteration
            )
            print(f"{task.id}: {task.status}")
    except (OSError, ValueError) as exc:
        raise SystemExit(f"taskbeat: {exc}") from exc

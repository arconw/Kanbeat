"""Kanbeat command entry point. All UI clients connect to the same supervisor."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys

from .filesystem import Board
from .supervisor import Supervisor, ensure_supervisor, request


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kanbeat")
    p.add_argument("--project", type=Path, default=Path.cwd(), help="Project directory")
    sub = p.add_subparsers(dest="command")
    sub.add_parser("run", help="Connect TUI to the local supervisor")
    sub.add_parser("supervisor", help="Run the supervisor in the foreground")
    sub.add_parser("play", help="Start/restart the board queue")
    sub.add_parser("stop", help="Interrupt board workers and stop dispatching")
    sub.add_parser("list", help="List ticket files by column")
    new = sub.add_parser("new", help="Create plain Markdown ticket")
    new.add_argument("title")
    new.add_argument("--description", default="")
    new.add_argument("--column", choices=("backlog", "todo"), default="backlog")
    move = sub.add_parser("move", help="Move ticket to Kanban column")
    move.add_argument("ticket")
    move.add_argument(
        "column",
        choices=("backlog", "todo", "progress", "review", "done", "failed", "archive"),
    )
    return p


async def _send(project: Path, payload: dict) -> dict:
    ensure_supervisor(project)
    return await request(project, payload)


def main() -> None:
    args = parser().parse_args()
    project = args.project.expanduser().resolve()
    try:
        if args.command == "supervisor":
            asyncio.run(Supervisor(project).serve())
        elif args.command in (None, "run"):
            ensure_supervisor(project)
            from .ui.app import KanbeatApp

            KanbeatApp(project).run()
        elif args.command == "list":
            board = Board(project)
            for ticket in board.scan()[0]:
                print(
                    f"{ticket.column:10} {ticket.order:5} {ticket.id:6} {ticket.title}"
                )
        elif args.command == "new":
            asyncio.run(
                _send(
                    project,
                    {
                        "op": "create",
                        "title": args.title,
                        "body": args.description,
                        "column": args.column,
                    },
                )
            )
        elif args.command == "move":
            asyncio.run(
                _send(
                    project,
                    {
                        "op": "move",
                        "ticket": args.ticket,
                        "column": args.column,
                    },
                )
            )
        else:
            asyncio.run(_send(project, {"op": args.command}))
    except (RuntimeError, ValueError, OSError) as exc:
        print("kanbeat: " + str(exc), file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()

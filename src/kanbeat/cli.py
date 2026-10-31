"""Kanbeat command entry point. All UI clients connect to the same supervisor."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys

from . import __version__
from .agent_cli import (
    KNOWN_KEYS,
    config_command,
    events_command,
    status_command,
    watch_command,
)
from .filesystem import Board
from .launcher import run_internal
from .ipc import socket_path
from .supervisor import Supervisor, ensure_supervisor, request


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kanbeat")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--project", type=Path, default=Path.cwd(), help="Project directory")
    sub = p.add_subparsers(dest="command")
    sub.add_parser("run", help="Connect TUI to the local supervisor")
    sub.add_parser("supervisor", help="Run the supervisor in the foreground")
    sub.add_parser("play", help="Start/restart the board queue")
    sub.add_parser("stop", help="Interrupt board workers and stop dispatching")
    shutdown = sub.add_parser(
        "shutdown", help="Stop this project supervisor and its sessions"
    )
    shutdown.add_argument(
        "--yes", action="store_true", help="Confirm stopping independent sessions"
    )
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
    status = sub.add_parser(
        "status", help="Show execution state and tickets per column"
    )
    status.add_argument("--json", action="store_true", help="Machine-readable output")
    watch = sub.add_parser(
        "watch", help="Print one line per ticket event until interrupted"
    )
    watch.add_argument("--json", action="store_true", help="One JSON object per line")
    watch.add_argument(
        "--interval", type=float, default=1.0, help="Seconds between checks"
    )
    watch.add_argument(
        "--since", type=int, help="Start after this event number (default: now)"
    )
    events = sub.add_parser("events", help="Print recent ticket events and exit")
    events.add_argument("--json", action="store_true", help="One JSON object per line")
    events.add_argument("--since", type=int, help="Only events after this number")
    events.add_argument("--limit", type=int, default=100, help="Most events to print")
    config = sub.add_parser(
        "config", help="Read and change project settings, with history"
    )
    actions = config.add_subparsers(dest="config_command", required=True)
    get = actions.add_parser("get", help="Print all settings or one setting")
    get.add_argument("key", nargs="?", help=KNOWN_KEYS)
    set_ = actions.add_parser("set", help="Change one setting")
    set_.add_argument("key", help=KNOWN_KEYS)
    set_.add_argument("value", nargs="?", help="New value, or - for standard input")
    set_.add_argument("--file", help="Read the new value from a file")
    history = actions.add_parser("history", help="List recent settings changes")
    history.add_argument("--limit", type=int, default=10)
    revert = actions.add_parser("revert", help="Undo a settings change")
    revert.add_argument("id", type=int)
    return p


async def _send(project: Path, payload: dict) -> dict:
    ensure_supervisor(project)
    return await request(project, payload)


async def _with_supervisor(project: Path, command, *arguments) -> None:
    ensure_supervisor(project)

    async def send(payload: dict) -> dict:
        return await request(project, payload)

    await command(send, *arguments)


async def shutdown(project: Path, confirmed: bool) -> None:
    await request(project, {"op": "shutdown", "confirmed": confirmed})
    for _ in range(400):
        if not socket_path(project).exists():
            return
        await asyncio.sleep(0.05)
    raise RuntimeError(
        "Shutdown was requested but not confirmed within 20 seconds; cleanup may "
        "still be running. Wait for the supervisor to exit before replacing binaries."
    )


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--native-bridge":
        from .native_events import main as bridge

        raise SystemExit(bridge(sys.argv[2:]))
    if run_internal(sys.argv):
        return
    try:
        run()
    except KeyboardInterrupt:
        raise SystemExit(130) from None


def run() -> None:
    args = parser().parse_args()
    project = args.project.expanduser().resolve()
    try:
        if args.command == "supervisor":
            asyncio.run(Supervisor(project).serve())
        elif args.command in (None, "run"):
            ensure_supervisor(project)
            from .ui.app import KanbeatApp

            KanbeatApp(project).run()
        elif args.command == "shutdown":
            asyncio.run(shutdown(project, args.yes))
        elif args.command == "config":
            asyncio.run(_with_supervisor(project, config_command, args))
        elif args.command == "status":
            asyncio.run(_with_supervisor(project, status_command, args.json))
        elif args.command == "watch":
            asyncio.run(
                _with_supervisor(
                    project,
                    watch_command,
                    args.json,
                    max(args.interval, 0.5),
                    args.since,
                )
            )
        elif args.command == "events":
            asyncio.run(
                _with_supervisor(
                    project, events_command, args.json, args.since, args.limit
                )
            )
        elif args.command == "list":
            board = Board(project)
            for ticket in board.scan()[0]:
                print(f"{ticket.column:10} {ticket.id:6} {ticket.title}")
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

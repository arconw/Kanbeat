from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config_keys import CONFIG_KEYS
from .events import format_event

Send = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
COLUMN_ORDER = ("backlog", "todo", "progress", "review", "done", "failed", "archive")
LINE_PREVIEW = 60
WATCH_RETRIES = 120
EVENT_BATCH = 200


def one_line(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value)
    text = " ".join(text.split())
    return repr(text[:LINE_PREVIEW] + ("…" if len(text) > LINE_PREVIEW else ""))


def read_value(value: str | None, source: str | None) -> str:
    if source is not None and value is not None:
        raise ValueError("Give either a value or --file, not both")
    if source is not None:
        return Path(source).read_text(encoding="utf-8")
    if value is None:
        raise ValueError("Give a value, --file PATH, or - to read standard input")
    return sys.stdin.read() if value == "-" else value


async def config_command(send: Send, args: Any) -> None:
    action = args.config_command
    if action == "get":
        result = await send({"op": "config_get", "key": args.key})
        if args.key is None:
            print(json.dumps(result["config"], indent=2, ensure_ascii=False))
        elif isinstance(result["value"], str):
            print(result["value"])
        else:
            print(json.dumps(result["value"]))
    elif action == "set":
        value = read_value(args.value, args.file)
        result = await send(
            {"op": "config_set", "changes": {args.key: value}, "source": "cli"}
        )
        report_entry(result["entry"], "changed")
    elif action == "history":
        result = await send({"op": "config_history", "limit": args.limit})
        for entry in result["history"]:
            print(f"#{entry['id']}  {entry['time']}  {entry['source']}")
            for change in entry["changes"]:
                print(
                    f"    {change['key']}: {one_line(change['old'])} -> "
                    f"{one_line(change['new'])}"
                )
        if not result["history"]:
            print("No config changes yet")
    elif action == "revert":
        result = await send({"op": "config_revert", "id": args.id, "source": "cli"})
        report_entry(result["entry"], "reverted")


def report_entry(entry: dict[str, Any] | None, verb: str) -> None:
    if entry is None:
        print("Nothing to change: the settings already have these values")
        return
    print(f"{verb}: {', '.join(entry['keys'])} (change #{entry['id']})")
    if entry.get("skipped"):
        print(
            f"kept: {', '.join(entry['skipped'])} (changed again later)",
            file=sys.stderr,
        )


def board_rows(snapshot: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    rows: dict[str, list[dict[str, Any]]] = {column: [] for column in COLUMN_ORDER}
    for ticket in snapshot.get("tickets", []):
        rows.setdefault(ticket["column"], []).append(
            {
                "id": ticket["id"],
                "title": ticket["title"],
                "blocked_by": [
                    row["id"]
                    for row in ticket.get("blocked_by", [])
                    if not row.get("satisfied")
                ],
            }
        )
    return rows


async def status_command(send: Send, as_json: bool) -> None:
    snapshot = await send({"op": "snapshot"})
    rows = board_rows(snapshot)
    execution = snapshot.get("execution", "stopped")
    if as_json:
        print(
            json.dumps(
                {"execution": execution, "columns": rows}, indent=2, ensure_ascii=False
            )
        )
        return
    print(f"execution: {execution}")
    for column, tickets in rows.items():
        if not tickets and column in ("failed", "archive"):
            continue
        print(f"{column} ({len(tickets)})")
        for ticket in tickets:
            waiting = (
                "  (blocked by "
                + ", ".join("#" + b for b in ticket["blocked_by"])
                + ")"
                if ticket["blocked_by"]
                else ""
            )
            print(f"  #{ticket['id']} {ticket['title']}{waiting}")


def emit(event: dict[str, Any], as_json: bool) -> None:
    if as_json:
        if "time" not in event:
            event = {
                "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                **event,
            }
        print(json.dumps(event, ensure_ascii=False), flush=True)
    else:
        print(format_event(event), flush=True)


async def events_command(
    send: Send, as_json: bool, since: int | None, limit: int
) -> int:
    if since is None:
        reply = await send({"op": "events", "tail": limit})
    else:
        reply = await send({"op": "events", "since": since, "limit": limit})
    for event in reply["events"]:
        emit(event, as_json)
    return reply["last"]


async def watch_command(
    send: Send,
    as_json: bool,
    interval: float,
    since: int | None = None,
    rounds: int | None = None,
    retries: int = WATCH_RETRIES,
) -> None:
    cursor = since
    failures = polls = 0
    while True:
        try:
            reply = await send({"op": "events", "since": cursor, "limit": EVENT_BATCH})
            events, last = reply["events"], reply["last"]
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            failures += 1
            if failures == 1:
                emit({"event": "disconnected"}, as_json)
            if failures > retries:
                raise RuntimeError("The supervisor stopped answering") from None
        else:
            if failures:
                emit({"event": "connected"}, as_json)
                failures = 0
            if cursor is None or last < cursor:
                cursor = last
            for event in events:
                emit(event, as_json)
                cursor = event["seq"]
        if rounds is not None and polls >= rounds:
            return
        polls += 1
        await asyncio.sleep(interval)


KNOWN_KEYS = ", ".join(CONFIG_KEYS)

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config_keys import CONFIG_KEYS

Send = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
COLUMN_ORDER = ("backlog", "todo", "progress", "review", "done", "failed", "archive")
LINE_PREVIEW = 60


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


def watch_index(snapshot: dict[str, Any]) -> tuple[dict[str, tuple[str, str]], str]:
    tickets = {
        ticket["id"]: (ticket["column"], ticket["title"])
        for ticket in snapshot.get("tickets", [])
    }
    return tickets, snapshot.get("execution", "stopped")


def diff_events(
    before: tuple[dict[str, tuple[str, str]], str],
    after: tuple[dict[str, tuple[str, str]], str],
) -> list[dict[str, Any]]:
    old, old_execution = before
    new, new_execution = after
    events: list[dict[str, Any]] = []
    for ticket_id in sorted(set(old) | set(new)):
        if ticket_id not in old:
            column, title = new[ticket_id]
            events.append(
                {
                    "event": "created",
                    "ticket": ticket_id,
                    "column": column,
                    "title": title,
                }
            )
        elif ticket_id not in new:
            events.append(
                {"event": "deleted", "ticket": ticket_id, "title": old[ticket_id][1]}
            )
        elif old[ticket_id][0] != new[ticket_id][0]:
            events.append(
                {
                    "event": "moved",
                    "ticket": ticket_id,
                    "from": old[ticket_id][0],
                    "to": new[ticket_id][0],
                    "title": new[ticket_id][1],
                }
            )
    if old_execution != new_execution:
        events.append(
            {"event": "execution", "from": old_execution, "to": new_execution}
        )
    return events


def format_event(event: dict[str, Any]) -> str:
    kind = event["event"]
    if kind == "created":
        return f"created #{event['ticket']} in {event['column']}  {event['title']}"
    if kind == "deleted":
        return f"deleted #{event['ticket']}  {event['title']}"
    if kind == "moved":
        return (
            f"moved #{event['ticket']} {event['from']} -> {event['to']}  "
            f"{event['title']}"
        )
    return f"execution {event['from']} -> {event['to']}"


async def watch_command(
    send: Send,
    as_json: bool,
    interval: float,
    rounds: int | None = None,
) -> None:
    before = watch_index(await send({"op": "snapshot"}))
    done = 0
    while rounds is None or done < rounds:
        await asyncio.sleep(interval)
        done += 1
        after = watch_index(await send({"op": "snapshot"}))
        for event in diff_events(before, after):
            if as_json:
                event = {
                    "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    **event,
                }
                print(json.dumps(event, ensure_ascii=False), flush=True)
            else:
                print(format_event(event), flush=True)
        before = after


KNOWN_KEYS = ", ".join(CONFIG_KEYS)

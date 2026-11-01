from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config_keys import CONFIG_KEYS, HARNESSES
from .events import format_event

Send = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
COLUMN_ORDER = ("backlog", "todo", "progress", "review", "done", "failed", "archive")
LINE_PREVIEW = 60
WATCH_RETRIES = 120
EVENT_BATCH = 200
RUNNING_WORKER_STATUSES = (
    "starting",
    "running",
    "stopping",
    "recovering",
    "recovery_blocked",
)
RESTART_HELP = (
    "Stop the board, apply the change, and play again only if it was running; "
    "this interrupts running workers, which resume from their native sessions"
)


def one_line(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value)
    text = " ".join(text.split())
    return repr(text[:LINE_PREVIEW] + ("…" if len(text) > LINE_PREVIEW else ""))


def read_value(value: str | None, source: str | None, flag: str = "--file") -> str:
    if source is not None and value is not None:
        raise ValueError(f"Give either a value or {flag}, not both")
    if source is not None:
        return Path(source).read_text(encoding="utf-8")
    if value is None:
        raise ValueError(f"Give a value, {flag} PATH, or - to read standard input")
    return sys.stdin.read() if value == "-" else value


def is_reviewer_slot(value: str) -> bool:
    digits = value[9:] if value.startswith("reviewer-") else ""
    return bool(digits) and digits.isascii() and digits.isdigit() and digits[0] != "0"


def check_slot(value: str) -> str:
    if value == "developer" or is_reviewer_slot(value):
        return value
    raise ValueError(
        f"Invalid slot {value!r}. Use developer or reviewer-N, for example reviewer-1"
    )


def check_reviewer(value: str) -> str:
    slot = check_slot(value)
    if slot == "developer":
        raise ValueError("The developer slot is not a Reviewer slot; use reviewer-N")
    return slot


def check_harness(value: str) -> str:
    if value not in HARNESSES:
        raise ValueError(
            f"Unknown harness {value!r}. Valid harnesses: " + ", ".join(HARNESSES)
        )
    return value


def reviewer_number(value: str) -> int | None:
    return int(value[9:]) if is_reviewer_slot(value) else None


def running_error() -> ValueError:
    return ValueError(
        "The board is running. Stop it with kanbeat stop, apply the change, then "
        "start it with kanbeat play; or pass --restart."
    )


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


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def ticket_reviewers(ticket: dict[str, Any]) -> list[dict[str, Any]]:
    state = as_dict(ticket.get("state"))
    return [
        row
        for row in state.get("reviewers") or []
        if isinstance(row, dict) and row.get("id")
    ]


def slot_row(
    ticket: dict[str, Any], slot: str, assignment: dict[str, Any], default: str
) -> dict[str, Any]:
    runtime = as_dict(as_dict(ticket.get("state")).get("runtime"))
    status = str(as_dict(runtime.get(slot)).get("status") or "")
    return {
        "slot": slot,
        "harness": str(assignment.get("harness") or default),
        "custom_prompt": bool(str(assignment.get("cp") or "")),
        "running": status in RUNNING_WORKER_STATUSES,
    }


def ticket_slots(ticket: dict[str, Any], default: str) -> dict[str, Any]:
    state = as_dict(ticket.get("state"))
    return {
        "developer": slot_row(
            ticket, "developer", as_dict(state.get("developer")), default
        ),
        "reviewers": [
            slot_row(ticket, str(row["id"]), row, default)
            for row in ticket_reviewers(ticket)
        ],
    }


def board_rows(snapshot: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    rows: dict[str, list[dict[str, Any]]] = {column: [] for column in COLUMN_ORDER}
    default = str(as_dict(snapshot.get("config")).get("default_harness") or "")
    for ticket in snapshot.get("tickets", []):
        slots = ticket_slots(ticket, default)
        rows.setdefault(ticket["column"], []).append(
            {
                "id": ticket["id"],
                "title": ticket["title"],
                "blocked_by": [
                    row["id"]
                    for row in ticket.get("blocked_by", [])
                    if not row.get("satisfied")
                ],
                "slots": slots,
                "straight_to_done": not slots["reviewers"],
            }
        )
    return rows


def slot_text(slot: dict[str, Any]) -> str:
    harness = slot["harness"] or "project default"
    custom = "custom prompt" if slot["custom_prompt"] else "no custom prompt"
    worker = "worker running" if slot["running"] else "no worker"
    return f"harness {harness}, {custom}, {worker}"


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
            print(f"    developer: {slot_text(ticket['slots']['developer'])}")
            for reviewer in ticket["slots"]["reviewers"]:
                print(f"    {reviewer['slot']}: {slot_text(reviewer)}")
            if ticket["straight_to_done"]:
                print(
                    "    no Reviewer slot: goes straight to Done when development "
                    "finishes"
                )


def ticket_index(
    snapshot: dict[str, Any], ticket_ids: list[str]
) -> dict[str, dict[str, Any]]:
    tickets = {
        str(ticket["id"]): ticket
        for ticket in snapshot.get("tickets", [])
        if ticket.get("id")
    }
    missing = [ticket_id for ticket_id in ticket_ids if ticket_id not in tickets]
    if missing:
        raise ValueError("Unknown ticket: " + ", ".join("#" + _id for _id in missing))
    return tickets


def changed_text(applied: list[int], requests: list[dict[str, Any]]) -> str:
    tickets = [str(requests[index].get("ticket", "")) for index in applied]
    if not tickets:
        return "No ticket was changed"
    return "Changed " + ", ".join("#" + ticket for ticket in tickets)


def with_applied(
    error: Exception,
    applied: list[int],
    requests: list[dict[str, Any]],
    play_error: Exception | None = None,
    play_failed: bool = False,
) -> Exception:
    summary = changed_text(applied, requests)
    if play_failed:
        message = f"{summary}, but the board could not be played again: {error}"
    else:
        message = f"{summary} before the change was refused: {error}"
        if play_error is not None:
            message += f". The board could not be played again: {play_error}"
    if isinstance(error, ValueError):
        return ValueError(message)
    if isinstance(error, RuntimeError):
        return RuntimeError(message)
    return error


def running_slot_terminals(
    snapshot: dict[str, Any], requests: list[dict[str, Any]]
) -> list[str]:
    terminals = snapshot.get("terminals")
    if not isinstance(terminals, dict):
        return []
    busy: list[str] = []
    for request in requests:
        ticket = str(request.get("ticket") or "")
        slot = str(request.get("slot") or "developer")
        for entry in terminals.values():
            if (
                isinstance(entry, dict)
                and entry.get("ticket") == ticket
                and entry.get("slot") == slot
                and entry.get("status") == "running"
            ):
                busy.append(f"#{ticket}/{slot}")
                break
    return busy


async def apply_requests(
    send: Send,
    snapshot: dict[str, Any],
    requests: list[dict[str, Any]],
    restart: bool,
) -> list[int]:
    was_running = snapshot.get("execution") == "running"
    if was_running and not restart:
        raise running_error()
    busy = running_slot_terminals(snapshot, requests)
    if busy:
        raise ValueError(
            "Stop the interactive slot terminal before changing its assignment: "
            + ", ".join(busy)
        )
    applied: list[int] = []
    failure: Exception | None = None
    play_failure: Exception | None = None
    if was_running:
        await send({"op": "stop"})
    try:
        try:
            for index, request in enumerate(requests):
                await send(request)
                applied.append(index)
        except Exception as error:
            failure = error
    finally:
        if was_running:
            try:
                await send({"op": "play"})
            except Exception as error:
                play_failure = error
    if failure is not None:
        raise with_applied(failure, applied, requests, play_failure) from failure
    if play_failure is not None:
        raise with_applied(
            play_failure, applied, requests, play_failed=True
        ) from play_failure
    return applied


def change_text(values: dict[str, str]) -> str:
    parts = []
    if "harness" in values:
        parts.append("harness " + values["harness"])
    if "cp" in values:
        parts.append("custom prompt" if values["cp"] else "no custom prompt")
    return ", ".join(parts)


async def assign_command(send: Send, args: Any) -> None:
    ticket_ids = list(dict.fromkeys(str(ticket) for ticket in args.tickets))
    slot = check_slot(str(args.slot))
    harness = None if args.harness is None else check_harness(str(args.harness))
    custom = None
    if args.cp is not None or args.cp_file is not None:
        custom = read_value(args.cp, args.cp_file, flag="--cp-file")
    if harness is None and custom is None:
        raise ValueError("Give --harness, --cp or --cp-file to change the slot")
    values: dict[str, str] = {}
    if harness is not None:
        values["harness"] = harness
    if custom is not None:
        values["cp"] = custom
    snapshot = await send({"op": "snapshot"})
    ticket_index(snapshot, ticket_ids)
    requests = [
        {"op": "set_assignment", "ticket": ticket_id, "slot": slot, "values": values}
        for ticket_id in ticket_ids
    ]
    await apply_requests(send, snapshot, requests, bool(args.restart))
    for ticket_id in ticket_ids:
        print(f"assigned {slot} on #{ticket_id}: {change_text(values)}")


async def reviewers_add_command(send: Send, args: Any) -> None:
    ticket_id = str(args.ticket)
    harness = check_harness(str(args.harness))
    snapshot = await send({"op": "snapshot"})
    ticket = ticket_index(snapshot, [ticket_id])[ticket_id]
    used = {
        number
        for row in ticket_reviewers(ticket)
        if (number := reviewer_number(str(row["id"]))) is not None
    }
    number = 1
    while number in used:
        number += 1
    slot = f"reviewer-{number}"
    request = {
        "op": "set_assignment",
        "ticket": ticket_id,
        "slot": slot,
        "values": {"harness": harness},
    }
    await apply_requests(send, snapshot, [request], bool(args.restart))
    print(f"assigned {slot} on #{ticket_id}: harness {harness}")


async def reviewers_remove_command(send: Send, args: Any) -> None:
    ticket_id = str(args.ticket)
    slot = check_reviewer(str(args.slot))
    snapshot = await send({"op": "snapshot"})
    ticket = ticket_index(snapshot, [ticket_id])[ticket_id]
    reviewers = ticket_reviewers(ticket)
    if not any(str(row["id"]) == slot for row in reviewers):
        raise ValueError(f"Ticket #{ticket_id} has no {slot} Reviewer slot")
    remaining = [row for row in reviewers if str(row["id"]) != slot]
    request = {"op": "remove_reviewer", "ticket": ticket_id, "slot": slot}
    await apply_requests(send, snapshot, [request], bool(args.restart))
    print(f"removed {slot} from #{ticket_id}")
    if not remaining:
        print(
            f"No Reviewer left on #{ticket_id}: it goes straight to Done when its "
            "development finishes."
        )


async def reviewers_command(send: Send, args: Any) -> None:
    if args.reviewers_command == "add":
        await reviewers_add_command(send, args)
    else:
        await reviewers_remove_command(send, args)


async def board_control_command(send: Send, action: str) -> None:
    result = await send({"op": action})
    print(f"execution: {result.get('execution', 'unknown')}")


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
    if reply.get("missed"):
        emit({"event": "gap", "missed": reply["missed"]}, as_json)
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
            elif reply.get("missed"):
                emit({"event": "gap", "missed": reply["missed"]}, as_json)
            for event in events:
                emit(event, as_json)
                cursor = event["seq"]
        if rounds is not None and polls >= rounds:
            return
        polls += 1
        await asyncio.sleep(interval)


KNOWN_KEYS = ", ".join(CONFIG_KEYS)

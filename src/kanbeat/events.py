from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .filesystem import Ticket
from .state import LocalState

EVENT_LIMIT = 200
Index = dict[str, Any]


def board_index(tickets: list[Ticket], execution: str) -> Index:
    return {
        "tickets": {ticket.id: [ticket.column, ticket.title] for ticket in tickets},
        "execution": execution,
    }


def diff_events(before: Index, after: Index) -> list[dict[str, Any]]:
    old, new = before["tickets"], after["tickets"]
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
    if before["execution"] != after["execution"]:
        events.append(
            {
                "event": "execution",
                "from": before["execution"],
                "to": after["execution"],
            }
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
    if kind in ("connected", "disconnected"):
        return f"supervisor {kind}"
    return f"execution {event['from']} -> {event['to']}"


class EventLog:
    def __init__(self, state: LocalState):
        self.state = state
        self.events: list[dict[str, Any]] = state.data.setdefault("events", [])

    @property
    def last(self) -> int:
        return int(self.state.data.get("event_seq", 0))

    def observe(self, tickets: list[Ticket], execution: str) -> list[dict[str, Any]]:
        current = board_index(tickets, execution)
        known = self.state.data.get("event_index")
        if known == current:
            return []
        self.state.data["event_index"] = current
        if known is None:
            return []
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        added = []
        for event in diff_events(known, current):
            serial = self.last + 1
            self.state.data["event_seq"] = serial
            added.append({"seq": serial, "time": stamp, **event})
        self.events.extend(added)
        del self.events[:-EVENT_LIMIT]
        return added

    def since(self, cursor: int, limit: int = EVENT_LIMIT) -> list[dict[str, Any]]:
        return [event for event in self.events if event["seq"] > cursor][:limit]

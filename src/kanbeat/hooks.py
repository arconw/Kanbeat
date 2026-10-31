from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from .events import format_event
from .native import available

if TYPE_CHECKING:
    from .supervisor import Supervisor

SESSION_TITLE = "Transition hook"
WAKE_QUIET = 8.0
WAKE_GAP = 15.0
START_QUIET = 3.0
START_TIMEOUT = 90.0
RETRY_DELAY = 60.0
WAKE_EVENTS = 30
PROMPT_BYTES = 16000
PREAMBLE = (
    "You are the Kanbeat board subscriber for this project. Kanbeat wakes you "
    "whenever a ticket is created, moved or deleted, or the board starts or "
    "stops, and tells you what happened. Do what your standing instructions "
    "say about those events and keep answers short. Before you change tickets "
    "or settings, read .kanbeat/INSTRUCTIONS.md. Do not repeat work you have "
    "already done for an earlier event."
)


def event_lines(events: list[dict[str, Any]]) -> str:
    return "\n".join(f"#{event['seq']} {format_event(event)}" for event in events)


def first_prompt(prompt: str, events: list[dict[str, Any]]) -> str:
    text = (
        f"{PREAMBLE}\n\nStanding instructions:\n{prompt}\n\n"
        f"Board events so far:\n{event_lines(events)}"
    )
    if len(text.encode()) > PROMPT_BYTES:
        raise ValueError("the hook prompt is too long to start the session")
    return text


def wake_text(events: list[dict[str, Any]]) -> str:
    first, last = events[0]["seq"], events[-1]["seq"]
    return (
        f"Kanbeat board events #{first} to #{last}:\n{event_lines(events)}\n"
        "Continue with your standing instructions. "
        f"Earlier events: kanbeat events --since {max(first - 1, 0)}."
    )


class HookSubscriber:
    def __init__(self, supervisor: Supervisor):
        self.supervisor = supervisor
        self.task: asyncio.Task | None = None
        self.last_wake = 0.0
        self.retry_at = 0.0
        self.reported: set[str] = set()

    @property
    def state(self) -> dict[str, Any]:
        return self.supervisor.runtime.data.setdefault("hook", {})

    def kick(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.guarded_step())

    async def stop(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    def session_entry(self) -> dict[str, Any] | None:
        return next(
            (
                entry
                for entry in self.supervisor.terminals.entries.values()
                if entry.get("owner") == "custom" and entry.get("role") == "hook"
            ),
            None,
        )

    def fail(self, message: str) -> None:
        self.retry_at = time.monotonic() + RETRY_DELAY
        if message not in self.reported:
            self.reported.add(message)
            self.supervisor.notice("Transition hook: " + message, "error")
            self.supervisor.save()

    async def guarded_step(self) -> None:
        try:
            await self.step()
        except asyncio.CancelledError:
            raise
        except (ValueError, RuntimeError, OSError) as exc:
            self.fail(str(exc))

    async def step(self) -> None:
        if self.supervisor.shutdown_requested.is_set():
            return
        config = self.supervisor.runtime.config.get("transition_hook") or {}
        prompt = str(config.get("prompt") or "").strip()
        if not prompt:
            if self.state.pop("cursor", None) is not None:
                self.supervisor.save()
            return
        events = self.supervisor.events
        if "cursor" not in self.state:
            self.state["cursor"] = events.last
            self.supervisor.save()
            return
        pending = events.since(self.state["cursor"], WAKE_EVENTS)
        now = time.monotonic()
        if not pending or now < self.retry_at or now - self.last_wake < WAKE_GAP:
            return
        entry = self.session_entry()
        if entry is None:
            harness = str(
                config.get("harness")
                or self.supervisor.runtime.config["default_harness"]
            )
            if not available(harness):
                self.fail(f"harness {harness} is unavailable")
                return
            await self.supervisor.dispatch(
                {
                    "op": "session_create",
                    "title": SESSION_TITLE,
                    "harness": harness,
                    "prompt": first_prompt(prompt, pending),
                    "role": "hook",
                }
            )
            self.delivered(pending)
            return
        session = self.supervisor.terminals.sessions.get(entry["id"])
        running = session is not None and entry.get("status") in ("starting", "running")
        if not running:
            if entry.get("status") == "stopping":
                return
            await self.supervisor.dispatch(
                {"op": "session_restart", "terminal": entry["id"]}
            )
            if not await self.settle(entry["id"]):
                self.fail("the session did not become ready after a restart")
                return
        elif session.quiet_for() < WAKE_QUIET:
            return
        await self.type_into(entry["id"], pending)

    async def settle(self, terminal_id: str) -> bool:
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            session = self.supervisor.terminals.sessions.get(terminal_id)
            entry = self.supervisor.terminals.entries.get(terminal_id, {})
            if (
                session is not None
                and entry.get("status") == "running"
                and session.quiet_for() >= START_QUIET
            ):
                return True
            await asyncio.sleep(0.5)
        return False

    async def type_into(self, terminal_id: str, pending: list[dict[str, Any]]) -> None:
        await self.supervisor.dispatch(
            {
                "op": "terminal_input",
                "terminal": terminal_id,
                "events": [
                    {"kind": "paste", "text": wake_text(pending)},
                    {"kind": "key", "key": "enter", "character": "\r"},
                ],
            }
        )
        self.delivered(pending)

    def delivered(self, pending: list[dict[str, Any]]) -> None:
        self.state["cursor"] = pending[-1]["seq"]
        self.last_wake = time.monotonic()
        self.retry_at = 0.0
        self.supervisor.save()

from __future__ import annotations

from typing import Any

from .filesystem import Ticket
from .native import NativeRun, available
from .state import LocalState


class TransitionHooks:
    def __init__(self, state: LocalState):
        self.state = state
        self.entries = state.data.setdefault("hooks", {})

    def enqueue(self, before: Ticket, after: Ticket) -> None:
        config = self.state.config.get("transition_hook") or {}
        if before.column == after.column or not config.get("prompt", "").strip():
            return
        serial = int(self.state.data.get("hook_seq", 0)) + 1
        self.state.data["hook_seq"] = serial
        self.entries[str(serial)] = {
            "ticket": after.id,
            "from": before.column,
            "to": after.column,
            "harness": config.get("harness") or self.state.config["default_harness"],
            "prompt": config["prompt"],
            "status": "pending",
        }
        self.state.save()

    def record_process(
        self, entry: dict[str, Any], pid: int, identity: str | None
    ) -> None:
        entry.update(pid=pid, process_identity=identity)
        self.state.save()

    async def execute(self, event_id: str, runs: dict[str, NativeRun]) -> None:
        entry = self.entries[event_id]
        if entry["status"] != "pending" or self.state.data["execution"] != "running":
            return
        key = "hook:" + event_id
        entry["status"] = "running"
        self.state.save()
        try:
            harness = entry["harness"]
            if not available(harness):
                raise ValueError("Hook harness unavailable: " + harness)
            ticket = self.state.board.find(entry["ticket"])
            path = ticket.path.relative_to(self.state.board.project).as_posix()
            prompt = "\n\n".join(
                [
                    entry["prompt"],
                    f"Kanbeat transition event {event_id}: ticket #{ticket.id} "
                    f"{entry['from']} -> {entry['to']}.",
                    f"Current canonical ticket path: {path}. Read it directly if needed.",
                    "This is an independent transition hook session. Do not edit, "
                    "rename, move or delete Kanbeat tickets, configuration or runtime "
                    "state. Do not read worker transcripts or custom prompts. "
                    "Do not change ticket status. External side effects must be idempotent.",
                ]
            )
            run = NativeRun(
                harness,
                prompt,
                None,
                self.state.board.project,
                self.state.board.root / ".state" / "logs" / f"hook-{event_id}.log",
                on_process=lambda pid, identity: self.record_process(
                    entry, pid, identity
                ),
            )
            runs[key] = run
            result = await run.execute()
            entry["status"] = (
                "interrupted"
                if run.stopping
                else ("completed" if result.ok else "failed")
            )
            entry["reason"] = result.reason
        except Exception as exc:
            run = runs.get(key)
            if run is not None:
                await run.stop()
            entry.update(status="failed", reason=str(exc))
        finally:
            runs.pop(key, None)
            entry.pop("pid", None)
            entry.pop("process_identity", None)
            if entry["status"] != "completed":
                self.state.notice(
                    f"Transition hook {event_id} for #{entry['ticket']}: "
                    f"{entry['status']}. {entry.get('reason', '')} "
                    "Ticket status preserved; hook will not be replayed automatically.",
                    "warning",
                )
            self.state.save()

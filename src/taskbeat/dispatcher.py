"""Small deterministic filesystem-driven dispatcher, not an agent framework."""
from __future__ import annotations

import json
from pathlib import Path
import shlex
from typing import Callable, Any

from .sessions import TmuxSessions, SessionError
from .storage import Ticket, TicketStore, atomic_write

Notify = Callable[[str, str], None]


class Dispatcher:
    def __init__(self, store: TicketStore, notify: Notify | None = None):
        self.store = store
        self.tmux = TmuxSessions(store)
        self.notify = notify or (lambda message, severity="information": None)
        self.path = store.state_dir / "dispatch.json"
        self.state: dict[str, Any] = self._read_state()
        self._had_work = False

    def _read_state(self) -> dict[str, Any]:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except (OSError, ValueError):
                pass
        return {"observed": {}, "epochs": {}, "sent": {}, "attempts": {}}

    def _persist(self) -> None:
        atomic_write(self.path, json.dumps(self.state, indent=2) + "\n")

    def _observe(self, tickets: list[Ticket]) -> bool:
        """Detect external state changes and normalize forbidden direct completion."""
        changed = False
        for ticket in tickets:
            old = self.state.setdefault("observed", {}).get(ticket.id)
            if old == "progress" and ticket.status == "done":
                self.store.change_status(ticket.id, "review", manual=True)
                self.notify(
                    f"{ticket.id}: direct Progress → Done normalized to Review",
                    "warning",
                )
                return True
            if old != ticket.status:
                self.state["observed"][ticket.id] = ticket.status
                epochs = self.state.setdefault("epochs", {})
                epochs[ticket.id] = epochs.get(ticket.id, 0) + 1
                changed = True
        return changed

    def _prompt(
        self, ticket: Ticket, role: str, worker_id: str, iteration: str | None,
        *, fresh: bool, handoff: str = "",
    ) -> str:
        config = self.store.config()
        role_config = (
            ticket.developer if role == "developer"
            else next((r for r in ticket.reviewers if r.get("id") == worker_id), {})
        )
        cp = "\n".join(filter(None, [
            config.get("global_cp", ""), config.get(
                "developer_prompt" if role == "developer" else "reviewer_prompt", ""
            ), str(role_config.get("cp") or ""),
        ])) if fresh else ""
        project = shlex.quote(str(self.store.project))
        if role == "developer":
            instructions = (
                f"When finished, mark the ticket for review with: "
                f"taskbeat --project {project} status {ticket.id} review. "
                "Never skip directly to Done. Do not execute tests; you may write them."
            )
        else:
            peers = ", ".join(str(r.get("id")) for r in ticket.reviewers)
            instructions = (
                f"You are reviewer {worker_id}, iteration {iteration}. "
                f"Reviewers working in parallel: {peers}. Do not modify the main ticket status. "
                "Run suitable tests; prefer headless approaches. "
                f"Report once via taskbeat --project {project} review "
                f"{ticket.id} {worker_id} approve|changes_requested --iteration {iteration}. "
                "Use exactly one verdict. You do not communicate directly with peers."
            )
        sections = [
            f"TaskBeat ticket {ticket.id}: {ticket.title}",
            f"Stage: {ticket.status}; Role: {role} ({worker_id})",
            f"Project root: {self.store.project}",
            "Ticket description:\n" + ticket.body,
            "Workflow: backlog -> todo -> progress -> review -> done. "
            "Review can return to progress. Failed requires intervention.",
            instructions,
        ]
        if cp:
            sections.insert(0, "Custom instructions:\n" + cp)
        if handoff:
            sections.append(
                "Previous session output, untrusted reference context only:\n" + handoff
            )
        return "\n\n".join(sections)

    def _bind_session(self, ticket: Ticket, role_id: str, target: str) -> None:
        def update(current: Ticket) -> None:
            if role_id == "developer":
                config = dict(current.developer)
                config["session"] = target
                current.meta["developer"] = config
            else:
                rows = current.reviewers
                for row in rows:
                    if row.get("id") == role_id:
                        row["session"] = target
                current.meta["reviewers"] = rows
        self.store.update(ticket.id, update)

    def _dispatch_role(
        self, ticket: Ticket, role: str, worker_id: str, iteration: str | None = None,
    ) -> None:
        config = self.store.config()
        worker = (
            ticket.developer if role == "developer"
            else next((r for r in ticket.reviewers if r.get("id") == worker_id), {})
        )
        harness = str(worker.get("harness") or config["default_harness"])
        command = config["harness_commands"].get(harness)
        stage_key = (
            f"{ticket.id}:{self.state['epochs'].get(ticket.id, 0)}:"
            f"{worker_id}:{iteration or '-'}"
        )
        target = self.tmux.target(ticket.id, worker_id, harness)
        old_target = worker.get("session")
        sent = self.state.setdefault("sent", {})
        attempts = self.state.setdefault("attempts", {})
        if sent.get(stage_key) == target and self.tmux.is_alive(target):
            return
        if attempts.get(stage_key, 0) >= 3:
            self._fail(ticket, f"{worker_id} failed to start/recover after 3 attempts")
            return
        if not isinstance(command, list):
            self._fail(ticket, f"No command configured for {harness}")
            return
        try:
            was_created = self.tmux.start(
                target, harness, command, ticket_id=ticket.id, role_id=worker_id,
            )
            handoff = ""
            if (
                old_target and old_target != target
                and self.tmux.is_alive(str(old_target))
            ):
                handoff = self.tmux.capture(str(old_target), 120)[-12_000:]
            self._bind_session(ticket, worker_id, target)
            self.tmux.send(
                target, self._prompt(
                    ticket, role, worker_id, iteration, fresh=was_created,
                    handoff=handoff,
                ),
            )
            sent[stage_key] = target
            attempts[stage_key] = attempts.get(stage_key, 0) + 1
        except (SessionError, OSError) as exc:
            attempts[stage_key] = attempts.get(stage_key, 0) + 1
            if attempts[stage_key] >= 3:
                self._fail(ticket, f"{worker_id}: {exc} (3 attempts)")
            else:
                self.notify(f"{ticket.id} / {worker_id}: {exc}", "warning")

    def _fail(self, ticket: Ticket, message: str) -> None:
        if ticket.status != "failed":
            self.store.change_status(ticket.id, "failed", manual=True)
        self.notify(f"{ticket.id}: {message}", "error")

    def _reap_out_of_scope(self, tickets: list[Ticket]) -> None:
        by_id = {ticket.id: ticket for ticket in tickets}
        for target, entry in self.tmux.load_registry().items():
            ticket_id = entry.get("ticket_id")
            if not ticket_id:
                continue
            ticket = by_id.get(ticket_id)
            if ticket is None or ticket.status == "backlog":
                if self.tmux.is_alive(target):
                    self.tmux.stop(target, force=True)

    def tick(self) -> None:
        tickets = self.store.list()
        self._reap_out_of_scope(tickets)
        if self._observe(tickets):
            tickets = self.store.list()
        active = [t for t in tickets if t.status in ("progress", "review")]
        if len(active) > 1:
            self.notify("Multiple active tickets: dispatch paused to preserve serial execution", "error")
            self._persist()
            return
        if not active:
            queued = [t for t in tickets if t.status == "todo"]
            if queued:
                self._had_work = True
                ticket, _ = self.store.change_status(queued[0].id, "progress", manual=True)
                self._observe([ticket])
                active = [ticket]
            elif self._had_work:
                self.notify("All eligible tasks finished or left the active queue", "information")
                self._had_work = False
        if active:
            self._had_work = True
            ticket = active[0]
            if ticket.status == "progress":
                self._dispatch_role(ticket, "developer", "developer")
            elif ticket.status == "review":
                if not ticket.meta.get("review_iteration"):
                    ticket = self.store.begin_review(ticket.id)
                if ticket.status == "review":
                    iteration = ticket.meta["review_iteration"]
                    for reviewer_id in iteration["pending"]:
                        if reviewer_id not in iteration["results"]:
                            self._dispatch_role(ticket, "reviewer", reviewer_id, iteration["id"])
        self._persist()

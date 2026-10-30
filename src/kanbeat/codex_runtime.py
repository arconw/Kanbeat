from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

from .codex_host import (
    CodexDisconnected,
    CodexHostError,
    CodexNotification,
    CodexRpcError,
    CodexTimeout,
    thread_missing,
)
from .native import Outcome
from .native_settings import NativeSettings

WAITING_FLAGS = frozenset({"waitingOnApproval", "waitingOnUserInput"})
MAX_ANSWER = 1_000_000
FAILURE_GRACE = 5.0


def _turn(params: dict[str, Any]) -> dict[str, Any]:
    value = params.get("turn")
    return value if isinstance(value, dict) else {}


def _turn_id(params: dict[str, Any]) -> str | None:
    value = _turn(params).get("id")
    if isinstance(value, str) and value:
        return value
    value = params.get("turnId")
    return value if isinstance(value, str) and value else None


def _item(params: dict[str, Any]) -> dict[str, Any]:
    value = params.get("item")
    return value if isinstance(value, dict) else {}


def _message(item: dict[str, Any]) -> str:
    return str(item.get("text") or "")[:MAX_ANSWER]


class CodexRun:
    def __init__(
        self,
        host: Any,
        prompt: str,
        session_id: str | None,
        cwd: Path,
        log_path: Path,
        *,
        settings: NativeSettings | None = None,
        on_session: Callable[[str], None] | None = None,
        on_activity: Callable[[str | None], None] | None = None,
    ):
        self.host = host
        self.prompt = prompt
        self.session_id = session_id
        self.cwd = cwd
        self.log_path = log_path
        self.settings = settings
        self.on_session = on_session or (lambda _value: None)
        self.on_activity = on_activity or (lambda _value: None)
        self.subscription = None
        self.turn_id: str | None = None
        self.started = False
        self.finished = False
        self.failed = False
        self.interrupted = False
        self.stopping = False
        self.answer = ""
        self.error = ""
        self.activity: str | None = None

    def activity_update(self, value: str | None) -> None:
        if value != self.activity:
            self.activity = value
            self.on_activity(value)

    async def stop(self) -> None:
        self.stopping = True
        if self.turn_id is not None and self.session_id is not None:
            try:
                await self.host.interrupt_turn(self.session_id, self.turn_id)
            except (CodexHostError, CodexTimeout, OSError, asyncio.TimeoutError):
                pass
        subscription, self.subscription = self.subscription, None
        if subscription is not None:
            subscription.close()

    def handle(self, notification: CodexNotification) -> None:
        method = notification.method
        params = notification.params
        if method == "turn/started":
            if _turn_id(params) != self.turn_id:
                return
            self.started = True
            self.activity_update("working")
            return
        if method == "turn/completed":
            if _turn_id(params) != self.turn_id:
                return
            turn = _turn(params)
            self.finished = True
            self.answer_from_turn(turn)
            status = str(turn.get("status") or "")
            if status == "completed":
                self.failed = False
                self.error = ""
                self.activity_update("idle")
                return
            self.failed = True
            error = turn.get("error")
            self.error = str(
                error.get("message") if isinstance(error, dict) else error or status
            )[:400]
            self.activity_update("interrupted" if status == "interrupted" else "error")
            if status == "interrupted":
                self.interrupted = True
            return
        if method == "thread/status/changed" and self.turn_id is not None:
            status = params.get("status")
            status = status if isinstance(status, dict) else {}
            kind = str(status.get("type") or "")
            if kind == "active":
                flags = status.get("activeFlags")
                waiting = any(
                    flag in WAITING_FLAGS
                    for flag in (flags if isinstance(flags, list) else [])
                )
                self.activity_update("waiting" if waiting else "working")
            elif kind == "idle" and self.finished:
                self.activity_update("idle")
            elif kind == "systemError":
                self.activity_update("error")
            return
        if method == "error":
            turn_id = params.get("turnId")
            if turn_id is not None and turn_id != self.turn_id:
                return
            error = params.get("error")
            self.error = str(
                error.get("message") if isinstance(error, dict) else error or "error"
            )[:400]
            if not params.get("willRetry"):
                self.failed = True
            self.activity_update("error")
            return
        if method in ("item/started", "item/completed"):
            turn_id = params.get("turnId")
            if turn_id is not None and turn_id != self.turn_id:
                return
            item = _item(params)
            kind = str(item.get("type") or "")
            if method == "item/completed" and kind == "agentMessage":
                text = _message(item)
                if text:
                    self.answer = text
            if method == "item/started" and kind in (
                "commandExecution",
                "reasoning",
                "mcpToolCall",
                "dynamicToolCall",
            ):
                self.activity_update("working")

    def answer_from_turn(self, turn: dict[str, Any]) -> None:
        items = turn.get("items")
        if not isinstance(items, list):
            return
        for item in items:
            if isinstance(item, dict) and item.get("type") == "agentMessage":
                text = _message(item)
                if text:
                    self.answer = text

    async def execute(self) -> Outcome:
        if self.stopping:
            return Outcome(False, "interrupted before start", self.session_id, "")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as log:
            log.write("[Kanbeat] Codex shared app-server turn. Starting.\n")
            log.flush()
            try:
                resumed = bool(self.session_id)
                if self.session_id:
                    try:
                        await self.host.observe_thread(self.session_id)
                    except CodexRpcError as error:
                        if thread_missing(error):
                            return Outcome(
                                False,
                                "Codex thread no longer exists",
                                self.session_id,
                                "",
                                resume_missing=True,
                            )
                        raise
                else:
                    metadata = await self.host.start_thread(
                        self.cwd,
                        model=self.settings.model if self.settings else None,
                        model_provider=self.settings.provider
                        if self.settings
                        else None,
                    )
                    self.session_id = metadata.thread_id
                    self.on_session(self.session_id)
                if self.stopping:
                    return Outcome(
                        False, "interrupted before turn", self.session_id, ""
                    )
                subscription = self.host.subscribe(self.session_id)
                self.subscription = subscription
                try:
                    turn = await self.host.start_turn(
                        self.session_id,
                        self.prompt,
                        model=self.settings.model
                        if self.settings and not resumed
                        else None,
                        effort=self.settings.effort
                        if self.settings and not resumed
                        else None,
                    )
                    response_id = str(turn.get("id") or "")
                    if not response_id:
                        return Outcome(
                            False,
                            "Codex turn/start returned no turn id",
                            self.session_id,
                            "",
                        )
                    self.turn_id = response_id
                    if self.stopping:
                        await self.stop()
                        return Outcome(
                            False, "interrupted during launch", self.session_id, ""
                        )
                    loop = asyncio.get_running_loop()
                    failed_deadline: float | None = None
                    while not self.finished:
                        try:
                            notification = await subscription.get(0.25)
                        except CodexTimeout:
                            if (
                                self.failed
                                and failed_deadline is not None
                                and loop.time() >= failed_deadline
                            ):
                                break
                            await asyncio.sleep(0)
                            continue
                        self.handle(notification)
                        if self.failed and failed_deadline is None:
                            failed_deadline = loop.time() + FAILURE_GRACE
                    if self.interrupted:
                        return Outcome(
                            False,
                            "interrupted by board Stop",
                            self.session_id,
                            self.answer,
                        )
                    outcome = Outcome(
                        not self.failed and not self.stopping,
                        self.error if self.failed else "completed",
                        self.session_id,
                        self.answer,
                    )
                    summary = self.answer or self.error
                    if summary:
                        log.write(summary[-3000:] + "\n")
                        log.flush()
                    return outcome
                finally:
                    if self.subscription is subscription:
                        self.subscription = None
                    subscription.close()
            except CodexDisconnected as error:
                if self.stopping:
                    return Outcome(
                        False, "interrupted by board Stop", self.session_id, self.answer
                    )
                return Outcome(
                    False,
                    f"Codex app-server disconnected: {error}",
                    self.session_id,
                    self.answer,
                )
            except (CodexHostError, CodexTimeout, OSError, ValueError) as error:
                return Outcome(
                    False,
                    f"Codex shared runtime error: {type(error).__name__}: {error}",
                    self.session_id,
                    self.answer,
                )
            finally:
                self.activity_update(None)

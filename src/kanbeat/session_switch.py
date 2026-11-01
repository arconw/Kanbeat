"""Switch an independent session to another harness without losing context.

The operation reads native history through the harness readers, plans exactly
the events the target has not received, and makes the target harness active only
after delivery is confirmed. Codex and OpenCode receive the envelope through
their own message APIs; Claude and Pi receive a short prompt that points at a
private context file, because their conversation cannot be passed on a command
line.

Failure leaves the source harness active. An unconfirmed delivery is recorded
as uncertain and is reconciled against the target history before a retry.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, replace
import os
from pathlib import Path
import re
import uuid
from typing import Any

from .claude_history import read_claude_history
from .claude_runtime import session_missing
from .codex_history import read_codex_history
from .codex_host import CodexHostError, CodexRpcError, thread_missing
from .config_keys import HARNESSES, HARNESS_LABELS
from .native import available
from .native_settings import cli_args
from .opencode_history import read_opencode_history
from .opencode_host import (
    OpenCodeHostError,
    OpenCodeHttpError,
    OpenCodeModel,
    OpenCodeTimeout,
)
from .pi_history import read_pi_history
from .session_transfer import (
    IMPORT_NOTE,
    ConversationEvent,
    HarnessHistory,
    TransferLedger,
    TransferPlan,
    find_import_markers,
    render_short_prompt,
)

AVAILABLE = "available"
MISSING = "missing"
UNREACHABLE = "unreachable"
HISTORY_OUTCOMES = (AVAILABLE, MISSING, UNREACHABLE)
NATIVE_STATUSES = (AVAILABLE, MISSING)

ACTIVE_SESSION_STATES = ("starting", "running", "stopping", "recovery_blocked")
EXCLUDED_ROLES = ("config", "hook")

MAX_TRANSFER_BYTES = 1024 * 1024
DELIVERY_TIMEOUT = 300.0
CONTEXT_DIRECTORY = "transfers"
ERROR_TAIL = 600
CONTEXT_NAME = re.compile(r"[A-Za-z0-9_-]{1,128}")


class SessionSwitchRefused(ValueError):
    """A switch that needs an explicit user choice or a safe retry."""

    def __init__(self, reason: str, message: str, *, details: Any = None):
        super().__init__(message)
        self.reason = reason
        self.details = details if isinstance(details, dict) else {}

    def to_dict(self) -> dict[str, Any]:
        return {"reason": self.reason, **self.details}


@dataclass(frozen=True)
class _History:
    harness: str
    outcome: str
    events: tuple[ConversationEvent, ...] = ()
    unsupported: tuple[str, ...] = ()
    complete: bool = False
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.outcome not in HISTORY_OUTCOMES:
            raise ValueError(f"Unsupported history outcome: {self.outcome!r}")

    @property
    def available(self) -> bool:
        return self.outcome == AVAILABLE

    def history(self) -> HarnessHistory:
        return HarnessHistory(self.harness, self.events, self.unsupported)


@dataclass(frozen=True)
class _Delivery:
    target_message_id: str
    context_path: Path | None = None


class _DeliveryUncertain(RuntimeError):
    pass


class _DeliveryMissing(RuntimeError):
    pass


class _DeliveryFailed(RuntimeError):
    pass


def native_sessions(entry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return the per-harness native session map of one session record."""
    rows = entry.get("native_sessions")
    result: dict[str, dict[str, Any]] = {}
    if isinstance(rows, dict):
        for harness, row in rows.items():
            if harness not in HARNESSES or not isinstance(row, dict):
                continue
            session_id = row.get("id")
            status = row.get("status")
            result[harness] = {
                "id": session_id if isinstance(session_id, str) else None,
                "status": status if status in NATIVE_STATUSES else AVAILABLE,
            }
    return result


def set_native_session(
    entry: dict[str, Any],
    harness: str,
    session_id: str | None,
    status: str = AVAILABLE,
) -> None:
    """Record one harness native session without touching the active harness."""
    if harness not in HARNESSES:
        raise ValueError("Unsupported harness: " + str(harness))
    if status not in NATIVE_STATUSES:
        raise ValueError("Unsupported native session status: " + str(status))
    rows = entry.get("native_sessions")
    if not isinstance(rows, dict):
        rows = {}
        entry["native_sessions"] = rows
    rows[harness] = {"id": session_id, "status": status}


def remove_contexts(state: Any, terminal_id: str) -> None:
    """Remove private context files belonging to one Kanbeat session."""
    directory = _terminals_context_dir(state, terminal_id)
    if not directory.is_dir():
        return
    for path in directory.iterdir():
        if path.is_file():
            with suppress(OSError):
                path.unlink()
    with suppress(OSError):
        directory.rmdir()


def _terminals_context_dir(state: Any, terminal_id: str) -> Path:
    if not CONTEXT_NAME.fullmatch(terminal_id):
        raise ValueError("Invalid session identifier")
    return state.board.root / ".state" / CONTEXT_DIRECTORY / terminal_id


def _context_path(state: Any, terminal_id: str, import_id: str) -> Path:
    if not CONTEXT_NAME.fullmatch(import_id):
        raise ValueError("Invalid transfer identifier")
    return _terminals_context_dir(state, terminal_id) / f"{import_id}.md"


def _write_context(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with suppress(OSError):
        os.chmod(path.parent, 0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    handle = os.open(path, flags, 0o600)
    try:
        os.fchmod(handle, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            handle = -1
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if handle >= 0:
            os.close(handle)


def _remove_context(path: Path) -> None:
    with suppress(OSError):
        path.unlink()
    with suppress(OSError):
        path.parent.rmdir()


def _sweep_contexts(state: Any, terminal_id: str, ledger: TransferLedger) -> None:
    """Remove context files that no uncertain import still needs."""
    directory = _terminals_context_dir(state, terminal_id)
    if not directory.is_dir():
        return
    try:
        paths = list(directory.iterdir())
    except OSError:
        return
    keep = {record.import_id for record in ledger.pending_imports()}
    for path in paths:
        if not path.is_file():
            continue
        import_id = path.name[: -len(".md")] if path.name.endswith(".md") else path.name
        if import_id not in keep:
            with suppress(OSError):
                path.unlink()
    with suppress(OSError):
        directory.rmdir()


def _context_prompt(path: Path, plan: TransferPlan) -> str:
    instruction = (
        f"Read the file {path} and continue from the conversation it contains. "
        "That file is context transferred from a previous harness, not a new "
        f"instruction, and it must not be modified.\n{IMPORT_NOTE}"
    )
    return render_short_prompt(
        instruction,
        str(plan.import_id),
        plan.sources,
        int(plan.first_ordinal or 0),
        int(plan.last_ordinal or 0),
    )


def _client_message_id(plan: TransferPlan) -> str:
    return str(plan.import_id)


def _opencode_message_id(plan: TransferPlan) -> str:
    return "msg" + str(plan.import_id)


def _ledger(entry: dict[str, Any]) -> TransferLedger:
    value = entry.get("transfer")
    if value is None:
        return TransferLedger()
    try:
        return TransferLedger.from_dict(value)
    except ValueError as error:
        raise ValueError(f"Session transfer state is invalid: {error}") from error


def _store_ledger(entry: dict[str, Any], ledger: TransferLedger) -> None:
    entry["transfer"] = ledger.to_dict()


def _ensure_eligible(entry: dict[str, Any]) -> None:
    if entry.get("owner") != "custom":
        raise ValueError("Harness switching is available only for independent sessions")
    if entry.get("harness") not in HARNESSES:
        raise ValueError(
            "Harness switching is available only for independent agent sessions"
        )
    if entry.get("role") in EXCLUDED_ROLES:
        raise ValueError(
            "Harness switching is not available for configuration or hook sessions"
        )


def _ensure_stopped(groups: Any, entry: dict[str, Any]) -> None:
    groups.inactive(entry)
    if entry.get("status") not in ACTIVE_SESSION_STATES:
        return
    raise ValueError("Stop the session before switching its harness")


def _source_session(entry: dict[str, Any], source: str) -> str:
    session_id = entry.get("native_session")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError(
            f"The active {source} session has no native session ID. "
            "Set one on the session card before switching harnesses"
        )
    return session_id


def _import_message_ids(ledger: TransferLedger, harness: str) -> tuple[str, ...]:
    return tuple(
        record.target_message_id
        for record in ledger.import_records(harness)
        if record.target_message_id is not None
    )


async def _read_history(
    groups: Any,
    harness: str,
    session_id: str,
    ledger: TransferLedger,
) -> _History:
    imports = _import_message_ids(ledger, harness)
    if harness == "codex":
        if groups.codex_host is None:
            raise ValueError("Codex host provider is unavailable")
        try:
            host = await groups.codex_host()
            result = await read_codex_history(
                host, session_id, import_message_ids=imports
            )
        except (
            CodexHostError,
            OSError,
            asyncio.TimeoutError,
            ValueError,
            AttributeError,
            TypeError,
        ) as error:
            raise ValueError(
                f"Codex history is temporarily unreachable: {_error_text(error)}"
            ) from error
        return _History(
            harness,
            result.outcome,
            result.events,
            result.unsupported,
            result.complete,
            result.error,
        )
    if harness == "opencode":
        if groups.opencode_host is None:
            raise ValueError("OpenCode host provider is unavailable")
        try:
            host = await groups.opencode_host()
            result = await read_opencode_history(
                host, session_id, import_message_ids=imports
            )
        except (
            OpenCodeHostError,
            OSError,
            asyncio.TimeoutError,
            ValueError,
            AttributeError,
            TypeError,
        ) as error:
            raise ValueError(
                f"OpenCode history is temporarily unreachable: {_error_text(error)}"
            ) from error
        return _History(
            harness,
            result.outcome,
            result.events,
            result.unsupported,
            result.complete,
            result.detail,
        )
    if harness == "claude":
        try:
            result = await asyncio.to_thread(
                read_claude_history,
                session_id,
                projects_dir=groups.claude_projects_dir(),
            )
        except (OSError, ValueError) as error:
            raise ValueError(
                f"Claude history is temporarily unreachable: {_error_text(error)}"
            ) from error
        return _History(
            harness,
            result.outcome,
            result.events,
            result.unsupported,
            result.complete,
            result.detail,
        )
    if harness == "pi":
        try:
            result = await asyncio.to_thread(
                read_pi_history,
                session_id,
                sessions_dir=groups.pi_sessions_dir(),
            )
        except (OSError, ValueError) as error:
            raise ValueError(
                f"Pi history is temporarily unreachable: {_error_text(error)}"
            ) from error
        return _History(
            harness,
            result.outcome,
            result.events,
            result.unsupported,
            result.complete,
            result.detail,
        )
    raise ValueError("Unsupported harness: " + str(harness))


def _error_text(error: BaseException) -> str:
    text = " ".join(str(error).split())
    return text[-ERROR_TAIL:] if text else type(error).__name__


def _progress(groups: Any, message: str, level: str = "information") -> None:
    report = getattr(groups, "progress", None)
    if callable(report):
        report(message, level)


def _harness_label(harness: Any) -> str:
    return HARNESS_LABELS.get(harness, str(harness))


async def _target_present(groups: Any, harness: str, session_id: str) -> bool:
    if harness == "codex":
        if groups.codex_host is None:
            raise ValueError("Codex host provider is unavailable")
        try:
            host = await groups.codex_host()
            await host.observe_thread(session_id)
        except CodexRpcError as error:
            if thread_missing(error):
                return False
            raise ValueError(
                "Could not check the saved Codex thread: " + _error_text(error)
            ) from error
        except (CodexHostError, OSError, asyncio.TimeoutError, ValueError) as error:
            raise ValueError(
                "Could not check the saved Codex thread: " + _error_text(error)
            ) from error
        return True
    if harness == "opencode":
        if groups.opencode_host is None:
            raise ValueError("OpenCode host provider is unavailable")
        try:
            host = await groups.opencode_host()
            await host.read_session(session_id)
        except OpenCodeHttpError as error:
            if error.status == 404:
                return False
            raise ValueError(
                "Could not check the saved OpenCode session: " + _error_text(error)
            ) from error
        except (OpenCodeHostError, OSError, asyncio.TimeoutError, ValueError) as error:
            raise ValueError(
                "Could not check the saved OpenCode session: " + _error_text(error)
            ) from error
        return True
    if harness == "claude":
        try:
            return bool(groups.claude_session_started(session_id))
        except (OSError, ValueError) as error:
            raise ValueError(
                "Could not check the saved Claude session: " + _error_text(error)
            ) from error
    if harness == "pi":
        read = await _read_history(groups, harness, session_id, TransferLedger())
        if read.outcome == AVAILABLE:
            return True
        if read.outcome == MISSING:
            return False
        raise ValueError(
            "Could not check the saved Pi session: " + str(read.detail or "")
        )
    raise ValueError("Unsupported harness: " + str(harness))


def _cannot_check_native_writer(harness: str, source: str, detail: str) -> ValueError:
    return ValueError(
        f"Could not check whether the {harness} native session is running: "
        f"{detail}. The source {source} session stays active; retry the switch "
        f"when the {harness} host is reachable"
    )


async def _ensure_no_native_writer(
    groups: Any, rows: dict[str, dict[str, Any]], source: str
) -> None:
    codex_ids = [
        row["id"]
        for harness, row in rows.items()
        if harness == "codex" and isinstance(row.get("id"), str) and row["id"]
    ]
    if codex_ids:
        if groups.codex_host is None:
            raise _cannot_check_native_writer(
                "Codex", source, "the Codex host provider is unavailable"
            )
        try:
            host = await groups.codex_host()
        except (
            CodexHostError,
            OSError,
            asyncio.TimeoutError,
            AttributeError,
            TypeError,
            ValueError,
        ) as error:
            raise _cannot_check_native_writer(
                "Codex", source, _error_text(error)
            ) from error
        read_thread = getattr(host, "read_thread", None)
        if read_thread is None:
            raise _cannot_check_native_writer(
                "Codex", source, "the Codex host has no thread reader"
            )
        for session_id in codex_ids:
            try:
                metadata = await read_thread(session_id)
            except CodexRpcError as error:
                if thread_missing(error):
                    continue
                raise _cannot_check_native_writer(
                    "Codex", source, _error_text(error)
                ) from error
            except (
                CodexHostError,
                OSError,
                asyncio.TimeoutError,
                AttributeError,
                TypeError,
                ValueError,
            ) as error:
                raise _cannot_check_native_writer(
                    "Codex", source, _error_text(error)
                ) from error
            if getattr(metadata, "status", None) == "active":
                raise ValueError(
                    "A Codex native session is still running; wait for it to "
                    "finish before switching harnesses"
                )
    identifiers = [
        row["id"]
        for harness, row in rows.items()
        if harness == "opencode" and isinstance(row.get("id"), str) and row["id"]
    ]
    if not identifiers:
        return
    if groups.opencode_host is None:
        raise _cannot_check_native_writer(
            "OpenCode", source, "the OpenCode host provider is unavailable"
        )
    try:
        host = await groups.opencode_host()
        statuses = await host.statuses()
    except (
        OpenCodeHostError,
        OSError,
        asyncio.TimeoutError,
        AttributeError,
        TypeError,
        ValueError,
    ) as error:
        raise _cannot_check_native_writer(
            "OpenCode", source, _error_text(error)
        ) from error
    if not isinstance(statuses, dict):
        raise _cannot_check_native_writer(
            "OpenCode", source, "the status response was malformed"
        )
    for session_id in identifiers:
        if session_id not in statuses:
            continue
        status = getattr(statuses[session_id], "status", None)
        if not isinstance(status, str) or not status:
            raise _cannot_check_native_writer(
                "OpenCode", source, "the session status was malformed"
            )
        if status != "idle":
            raise ValueError(
                "An OpenCode native session is still running; stop it before "
                "switching harnesses"
            )


def _assign_ordinals(
    entry: dict[str, Any],
    reads: list[_History],
) -> dict[str, _History]:
    buckets = entry.get("transfer_ordinals")
    if not isinstance(buckets, dict):
        buckets = {}
        entry["transfer_ordinals"] = buckets
    sequence = entry.get("transfer_ordinal_seq")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        sequence = 0
        for harness_rows in buckets.values():
            if isinstance(harness_rows, dict):
                for value in harness_rows.values():
                    if isinstance(value, int) and not isinstance(value, bool):
                        sequence = max(sequence, value + 1)
    updated: dict[str, _History] = {}
    for read in reads:
        harness_rows = buckets.get(read.harness)
        if not isinstance(harness_rows, dict):
            harness_rows = {}
            buckets[read.harness] = harness_rows
        events: list[ConversationEvent] = []
        for event in read.events:
            key = event.key[1]
            ordinal = harness_rows.get(key)
            if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
                ordinal = sequence
                sequence += 1
                harness_rows[key] = ordinal
            events.append(replace(event, ordinal=ordinal))
        updated[read.harness] = replace(read, events=tuple(events))
    entry["transfer_ordinal_seq"] = sequence
    return updated


def _require_complete(read: _History, *, source: bool) -> None:
    label = "source" if source else "recorded"
    if read.outcome == MISSING:
        raise ValueError(
            f"The {label} {read.harness} session no longer exists, so its history "
            "cannot be read. The active session was not changed"
        )
    if read.outcome == UNREACHABLE:
        detail = f": {read.detail}" if read.detail else ""
        raise ValueError(
            f"The {label} {read.harness} session is temporarily unreachable{detail}. "
            "The active session was not changed; retry when the harness is reachable"
        )
    if not read.complete:
        raise ValueError(
            f"The {label} {read.harness} history is incomplete, so switching would "
            "silently lose events. The active session was not changed"
        )


def _apply_reduction(plan: TransferPlan, reduction: Any) -> TransferPlan:
    if not isinstance(reduction, dict):
        raise ValueError("A transfer reduction must be an object")
    kind = reduction.get("kind")
    if kind == "full":
        return plan
    if kind == "text_only":
        return plan.reduce_to_roles()
    if kind == "newest":
        count = reduction.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError("A newest-events reduction needs a positive count")
        return plan.reduce_to_newest(count)
    raise ValueError("Unknown transfer reduction: " + str(kind))


def _refuse_large(plan: TransferPlan, limit: int) -> None:
    report = plan.size_report()
    options = [option.to_dict() for option in report.options]
    choice = (
        "Choose one of the explicit smaller transfers or cancel."
        if options
        else "No smaller transfer can be offered automatically; cancel or shorten "
        "the conversation in the source harness."
    )
    raise SessionSwitchRefused(
        "transfer_too_large",
        (
            f"The planned conversation is {plan.size_bytes} bytes, above the "
            f"{limit}-byte transfer limit. Nothing was truncated. {choice} "
            "A reduced transfer still has to fit the target model's context "
            "window"
        ),
        details={
            "size": report.to_dict(),
            "options": options,
            "unsupported": list(plan.unsupported),
        },
    )


def _mark_pending_not_delivered(
    entry: dict[str, Any],
    ledger: TransferLedger,
    state: Any,
    terminal_id: str,
    target: str,
) -> None:
    for record in ledger.pending_imports(target):
        ledger.mark_not_delivered(record.import_id)
        _remove_context(_context_path(state, terminal_id, record.import_id))
    _store_ledger(entry, ledger)


async def _reconcile_target(
    groups: Any,
    entry: dict[str, Any],
    ledger: TransferLedger,
    terminal_id: str,
    target: str,
    target_id: str | None,
) -> None:
    pending = ledger.pending_imports(target)
    if not pending:
        return
    if target_id is None:
        _mark_pending_not_delivered(entry, ledger, groups.state, terminal_id, target)
        groups.state.save()
        return
    read = await _read_history(groups, target, target_id, ledger)
    if read.outcome == MISSING:
        _mark_pending_not_delivered(entry, ledger, groups.state, terminal_id, target)
        groups.state.save()
        return
    if read.outcome != AVAILABLE:
        raise ValueError(
            f"Delivery to {target} from an earlier attempt is still unconfirmed "
            f"and its history could not be read: {read.detail or read.outcome}. "
            "The source session stays active; retry when the target is reachable"
        )
    result = ledger.reconcile(target, read.events, history_complete=read.complete)
    for record in (*result.confirmed, *result.missing):
        _remove_context(_context_path(groups.state, terminal_id, record.import_id))
    _store_ledger(entry, ledger)
    groups.state.save()
    if result.uncertain:
        raise ValueError(
            f"Delivery to {target} from an earlier attempt is still unconfirmed "
            "and the target history is incomplete. The source session stays "
            "active; retry when the complete target history can be read"
        )


async def _create_target(
    groups: Any, target: str, entry: dict[str, Any]
) -> tuple[str, bool]:
    settings = groups.fresh_settings(target)
    if target == "codex":
        if groups.codex_host is None:
            raise ValueError("Codex host provider is unavailable")
        try:
            host = await groups.codex_host()
            metadata = await groups.create_codex_thread(
                host, str(entry.get("title") or "Kanbeat session"), settings
            )
        except (CodexHostError, OSError, ValueError) as error:
            raise ValueError(
                f"Could not prepare a Codex session: {_error_text(error)}"
            ) from error
        return metadata.thread_id, True
    if target == "opencode":
        if groups.opencode_host is None:
            raise ValueError("OpenCode host provider is unavailable")
        try:
            host = await groups.opencode_host()
            model = (
                OpenCodeModel(settings.provider, settings.model)
                if settings is not None and settings.provider and settings.model
                else None
            )
            metadata = await groups.create_opencode_session(host, model)
        except (OpenCodeHostError, OSError, ValueError) as error:
            raise ValueError(
                f"Could not prepare an OpenCode session: {_error_text(error)}"
            ) from error
        return metadata.session_id, True
    if target in ("claude", "pi"):
        session_id = str(uuid.uuid4())
        return session_id, True
    raise ValueError("Unsupported harness: " + str(target))


def _fresh_state(
    groups: Any,
    entry: dict[str, Any],
    ledger: TransferLedger,
    terminal_id: str,
    target: str,
) -> None:
    ledger.reset_checkpoint(target)
    _mark_pending_not_delivered(entry, ledger, groups.state, terminal_id, target)
    rows = entry.get("native_sessions")
    if not isinstance(rows, dict):
        rows = {}
    rows.pop(target, None)
    entry["native_sessions"] = rows
    _store_ledger(entry, ledger)


async def switch_harness(groups: Any, request: dict[str, Any]) -> dict[str, Any]:
    """Implement the ``session_switch_harness`` supervisor operation."""
    terminal_id = str(request.get("terminal") or "")
    entry = groups.custom(terminal_id)
    target = request.get("harness")
    if target not in HARNESSES:
        raise ValueError("Unsupported harness: " + str(target))
    _ensure_eligible(entry)
    source = entry.get("harness")
    if target == source:
        return {
            "terminal": terminal_id,
            "harness": source,
            "native_session": entry.get("native_session"),
            "changed": False,
            "delivered": 0,
            "import_id": None,
            "unsupported": [],
            "reason": "already_active",
        }
    check_available = getattr(groups, "harness_available", None)
    installed = available(target)
    if not installed and check_available is not None:
        installed = check_available(target)
    if not installed:
        raise ValueError("Independent session harness unavailable: " + str(target))
    _ensure_stopped(groups, entry)
    source_id = _source_session(entry, str(source))

    rows = native_sessions(entry)
    rows[source] = {"id": source_id, "status": AVAILABLE}
    entry["native_sessions"] = rows
    ledger = _ledger(entry)
    _sweep_contexts(groups.state, terminal_id, ledger)

    await _ensure_no_native_writer(groups, rows, str(source))

    target_row = rows.get(target)
    target_id = target_row.get("id") if isinstance(target_row, dict) else None
    if not isinstance(target_id, str) or not target_id:
        target_id = None
    fresh = request.get("fresh_session") is True or request.get("fresh") is True

    if target_id is not None:
        try:
            present = await _target_present(groups, target, target_id)
        except ValueError as error:
            raise ValueError(
                f"{error}. The source {source} session stays active; retry the "
                "switch when the target harness is reachable"
            ) from error
        if not present:
            rows[target] = {"id": target_id, "status": MISSING}
            entry["native_sessions"] = rows
            _mark_pending_not_delivered(
                entry, ledger, groups.state, terminal_id, target
            )
            groups.state.save()
            if not fresh:
                raise SessionSwitchRefused(
                    "target_session_missing",
                    (
                        f"The saved {target} session {target_id} no longer exists. "
                        "Choose fresh_session to create a new native session and "
                        "transfer the accumulated conversation, or cancel. An "
                        "existing session is never replaced automatically"
                    ),
                    details={
                        "target": target,
                        "native_session": target_id,
                        "options": [
                            {
                                "kind": "fresh_session",
                                "description": (
                                    f"Start a new {target} session and transfer "
                                    "the accumulated conversation"
                                ),
                            }
                        ],
                    },
                )
            _fresh_state(groups, entry, ledger, terminal_id, target)
            groups.state.save()
            target_id = None
        else:
            rows[target] = {"id": target_id, "status": AVAILABLE}
            entry["native_sessions"] = rows
    else:
        if fresh:
            _fresh_state(groups, entry, ledger, terminal_id, target)
            groups.state.save()

    _progress(groups, "Reading conversation history")
    reads: list[_History] = []
    try:
        for harness, row in rows.items():
            if harness == target or harness == source:
                continue
            session_id = row.get("id")
            if not isinstance(session_id, str) or not session_id:
                continue
            if row.get("status") == MISSING:
                continue
            read = await _read_history(groups, harness, session_id, ledger)
            if read.outcome == MISSING:
                rows[harness] = {"id": session_id, "status": MISSING}
                entry["native_sessions"] = rows
                groups.state.save()
                continue
            _require_complete(read, source=False)
            reads.append(read)
        source_read = await _read_history(groups, str(source), source_id, ledger)
        if source_read.outcome == MISSING:
            rows[source] = {"id": source_id, "status": MISSING}
            entry["native_sessions"] = rows
            groups.state.save()
        _require_complete(source_read, source=True)
    except ValueError as error:
        message = str(error)
        if "stays active" not in message and "was not changed" not in message:
            raise ValueError(
                f"{message}. The source {source} session stays active; retry the "
                "switch when the harness is reachable"
            ) from error
        raise
    reads.append(source_read)

    sorted_reads = _assign_ordinals(entry, reads)
    histories = [read.history() for read in sorted_reads.values()]

    await _reconcile_target(groups, entry, ledger, terminal_id, str(target), target_id)

    plan = ledger.plan(histories, str(target))
    reduction = request.get("reduction")
    if reduction is not None:
        plan = _apply_reduction(plan, reduction)
        if plan.events == () and ledger.plan(histories, str(target)).events:
            raise ValueError(
                "The selected reduction removed every event; choose another "
                "transfer or cancel"
            )
    if plan.size_bytes > MAX_TRANSFER_BYTES:
        _refuse_large(plan, MAX_TRANSFER_BYTES)

    _progress(groups, "Preparing the target session")
    created = False
    if target_id is None:
        try:
            target_id, created = await _create_target(groups, str(target), entry)
        except ValueError as error:
            raise ValueError(
                f"{error}. The source {source} session stays active; retry the "
                "switch after the target harness is reachable"
            ) from error
        rows[target] = {"id": target_id, "status": AVAILABLE}
        entry["native_sessions"] = rows
        groups.state.save()

    transfer: dict[str, Any] = {
        "first_ordinal": plan.first_ordinal,
        "last_ordinal": plan.last_ordinal,
        "characters": plan.size.characters,
        "bytes": plan.size.bytes,
    }
    if not plan.events:
        entry["harness"] = target
        entry["native_session"] = target_id
        if any(read.events for read in reads):
            entry.pop("prompt", None)
        if created and target in ("claude", "pi"):
            entry["native_assigned"] = True
        else:
            entry.pop("native_assigned", None)
        _store_ledger(entry, ledger)
        groups.state.save()
        return {
            "terminal": terminal_id,
            "harness": target,
            "native_session": target_id,
            "changed": True,
            "delivered": 0,
            "import_id": None,
            "unsupported": list(plan.unsupported),
            "fresh_session": created,
            "transfer": transfer,
        }

    context_path: Path | None = None
    chosen_id: str | None = None
    if target == "codex":
        chosen_id = _client_message_id(plan)
    elif target == "opencode":
        chosen_id = _opencode_message_id(plan)
    else:
        context_path = _context_path(groups.state, terminal_id, str(plan.import_id))
        try:
            _write_context(context_path, plan.text)
        except OSError as error:
            _remove_context(context_path)
            raise ValueError(
                f"Could not prepare the transfer context file: {error}. "
                f"The source {source} session stays active; retry the switch"
            ) from error
    record = ledger.begin_delivery(plan, chosen_id)
    _store_ledger(entry, ledger)
    groups.state.save()

    _progress(groups, f"Delivering the conversation to {_harness_label(target)}")
    try:
        delivery = await _deliver(
            groups, str(target), target_id, plan, context_path, created
        )
    except _DeliveryMissing as error:
        ledger.mark_not_delivered(record.import_id)
        rows[target] = {"id": target_id, "status": MISSING}
        entry["native_sessions"] = rows
        _store_ledger(entry, ledger)
        if context_path is not None:
            _remove_context(context_path)
        groups.state.save()
        raise SessionSwitchRefused(
            "target_session_missing",
            (
                f"The target {target} session disappeared before delivery. "
                f"{_error_text(error)} Choose fresh_session to create a new "
                "native session and transfer the accumulated conversation, or "
                "cancel"
            ),
            details={
                "target": target,
                "native_session": target_id,
                "options": [
                    {
                        "kind": "fresh_session",
                        "description": (
                            f"Start a new {target} session and transfer the "
                            "accumulated conversation"
                        ),
                    }
                ],
            },
        ) from error
    except _DeliveryUncertain as error:
        _store_ledger(entry, ledger)
        groups.state.save()
        raise ValueError(
            f"Delivery to {target} was not confirmed: {_error_text(error)}. "
            f"The source {source} session stays active. Retry the switch; before "
            "sending again Kanbeat checks the target history for the import marker"
        ) from error
    except _DeliveryFailed as error:
        ledger.mark_not_delivered(record.import_id)
        _store_ledger(entry, ledger)
        if context_path is not None:
            _remove_context(context_path)
        groups.state.save()
        raise ValueError(
            f"Nothing was delivered to {target}: {_error_text(error)}. "
            f"The source {source} session stays active; retry the switch"
        ) from error
    except Exception as error:
        _store_ledger(entry, ledger)
        groups.state.save()
        raise ValueError(
            f"Delivery to {target} could not be completed: {_error_text(error)}. "
            f"The source {source} session stays active. Retry the switch; before "
            "sending again Kanbeat checks the target history for the import marker"
        ) from error

    ledger.confirm_delivery(record.import_id, delivery.target_message_id)
    entry["harness"] = target
    entry["native_session"] = target_id
    entry.pop("prompt", None)
    entry.pop("native_assigned", None)
    _store_ledger(entry, ledger)
    groups.state.save()
    if context_path is not None:
        _remove_context(context_path)
    return {
        "terminal": terminal_id,
        "harness": target,
        "native_session": target_id,
        "changed": True,
        "delivered": len(plan.events),
        "import_id": record.import_id,
        "target_message_id": delivery.target_message_id,
        "unsupported": list(plan.unsupported),
        "fresh_session": created,
        "transfer": transfer,
    }


async def _deliver(
    groups: Any,
    target: str,
    session_id: str,
    plan: TransferPlan,
    context_path: Path | None,
    created: bool,
) -> _Delivery:
    if target == "codex":
        return await _deliver_codex(groups, session_id, plan)
    if target == "opencode":
        return await _deliver_opencode(groups, session_id, plan)
    if context_path is None:
        raise _DeliveryFailed("The transfer context file is missing")
    prompt = _context_prompt(context_path, plan)
    if target == "claude":
        return await _deliver_claude(
            groups, session_id, plan, prompt, context_path, created
        )
    if target == "pi":
        return await _deliver_pi(
            groups, session_id, plan, prompt, context_path, created
        )
    raise ValueError("Unsupported harness: " + str(target))


async def _deliver_codex(
    groups: Any,
    session_id: str,
    plan: TransferPlan,
) -> _Delivery:
    if groups.codex_host is None:
        raise _DeliveryFailed("Codex host provider is unavailable")
    try:
        host = await groups.codex_host()
        await host.start_turn(
            session_id,
            plan.text,
            client_user_message_id=_client_message_id(plan),
        )
    except CodexRpcError as error:
        if thread_missing(error):
            raise _DeliveryMissing(_error_text(error)) from error
        raise _DeliveryUncertain(_error_text(error)) from error
    except (CodexHostError, OSError, asyncio.TimeoutError) as error:
        raise _DeliveryUncertain(_error_text(error)) from error
    return _Delivery(_client_message_id(plan))


async def _deliver_opencode(
    groups: Any,
    session_id: str,
    plan: TransferPlan,
) -> _Delivery:
    if groups.opencode_host is None:
        raise _DeliveryFailed("OpenCode host provider is unavailable")
    try:
        host = await groups.opencode_host()
        payload = await host.send_message(
            session_id,
            message_id=_opencode_message_id(plan),
            text=plan.text,
            system=IMPORT_NOTE,
            no_reply=True,
        )
    except OpenCodeHttpError as error:
        if error.status == 404:
            raise _DeliveryMissing(_error_text(error)) from error
        if error.status >= 500:
            raise _DeliveryUncertain(_error_text(error)) from error
        raise _DeliveryFailed(_error_text(error)) from error
    except OpenCodeTimeout as error:
        raise _DeliveryUncertain(_error_text(error)) from error
    except (OpenCodeHostError, OSError) as error:
        raise _DeliveryUncertain(_error_text(error)) from error
    info = payload.get("info")
    identifier = info.get("id") if isinstance(info, dict) else None
    if isinstance(identifier, str) and identifier:
        return _Delivery(identifier)
    return _Delivery(_opencode_message_id(plan))


async def _deliver_claude(
    groups: Any,
    session_id: str,
    plan: TransferPlan,
    prompt: str,
    context_path: Path,
    created: bool,
) -> _Delivery:
    argv = ["claude"]
    if created:
        argv += cli_args("claude", groups.fresh_settings("claude"), interactive=True)
        argv += ["--session-id", session_id]
    else:
        argv += ["--resume", session_id]
    argv += ["-p", prompt]
    environment = (
        groups.claude_runtime.child_env() if groups.claude_runtime is not None else None
    )
    code, output = await _one_shot(argv, groups.state.board.project, environment)
    if code is None:
        raise _DeliveryUncertain(output)
    if code != 0:
        if not created and session_missing(output):
            raise _DeliveryMissing(output or "Claude session not found")
        raise _DeliveryUncertain(output or f"Claude exited with status {code}")
    try:
        event = await _delivered_event(groups, "claude", session_id, plan)
    except ValueError as error:
        raise _DeliveryUncertain(_error_text(error)) from error
    if event is None:
        raise _DeliveryUncertain(
            "Claude finished but the import marker is not visible yet"
        )
    return _Delivery(event.event_id or str(plan.import_id), context_path)


async def _deliver_pi(
    groups: Any,
    session_id: str,
    plan: TransferPlan,
    prompt: str,
    context_path: Path,
    created: bool,
) -> _Delivery:
    argv = ["pi", "-p"]
    if created:
        argv += cli_args("pi", groups.fresh_settings("pi"), interactive=True)
    argv += ["--session-id", session_id, prompt]
    code, output = await _one_shot(argv, groups.state.board.project, None)
    if code is None:
        raise _DeliveryUncertain(output)
    if code != 0:
        raise _DeliveryUncertain(output or f"Pi exited with status {code}")
    try:
        event = await _delivered_event(groups, "pi", session_id, plan)
    except ValueError as error:
        raise _DeliveryUncertain(_error_text(error)) from error
    if event is None:
        raise _DeliveryUncertain("Pi finished but the import marker is not visible yet")
    return _Delivery(event.event_id or str(plan.import_id), context_path)


async def _terminate_delivery_process(
    process: asyncio.subprocess.Process,
) -> None:
    with suppress(ProcessLookupError):
        process.kill()
    with suppress(ProcessLookupError):
        await process.wait()


async def _one_shot(
    argv: list[str],
    cwd: Path,
    environment: dict[str, str] | None,
) -> tuple[int | None, str]:
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
        )
    except OSError as error:
        raise _DeliveryFailed(_error_text(error)) from error
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), DELIVERY_TIMEOUT)
    except asyncio.TimeoutError:
        await _terminate_delivery_process(process)
        return None, "the harness did not finish before the delivery timeout"
    except asyncio.CancelledError:
        await _terminate_delivery_process(process)
        raise
    text = (stderr + stdout).decode("utf-8", "replace")
    return process.returncode, " ".join(text.split())[-ERROR_TAIL:]


async def _delivered_event(
    groups: Any,
    harness: str,
    session_id: str,
    plan: TransferPlan,
) -> ConversationEvent | None:
    ledger = TransferLedger()
    read = await _read_history(groups, harness, session_id, ledger)
    if not read.available:
        return None
    import_id = plan.import_id
    for event in read.events:
        if event.import_id == import_id:
            return event
        if import_id is not None and any(
            marker.import_id == import_id for marker in find_import_markers(event.text)
        ):
            return event
    return None

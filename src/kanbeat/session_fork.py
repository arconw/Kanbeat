"""Fork an independent session into a new card on any harness.

The fork reads the whole conversation of the source card, creates a brand-new
native session on the chosen harness and delivers the conversation there with
the same transfer machinery that harness switching uses. The source card, its
native sessions and its transfer state are never modified. The new card is
registered only after delivery is confirmed, so a failed fork leaves nothing
behind on the board.
"""

from __future__ import annotations

import copy
import uuid
from contextlib import suppress
from typing import Any

from .config_keys import HARNESSES
from .native import available
from .session_switch import (
    AVAILABLE,
    MISSING,
    _apply_reduction,
    _assign_ordinals,
    _client_message_id,
    _context_path,
    _create_target,
    _deliver,
    _DeliveryFailed,
    _DeliveryMissing,
    _DeliveryUncertain,
    _ensure_eligible,
    _ensure_no_native_writer,
    _ensure_stopped,
    _error_text,
    _harness_label,
    _ledger,
    _opencode_message_id,
    _progress,
    _read_history,
    _remove_context,
    _require_complete,
    _store_ledger,
    _write_context,
    native_sessions,
)
from .session_transfer import TransferLedger


def _source_session(entry: dict[str, Any], harness: str) -> str:
    session_id = entry.get("native_session")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError(
            f"The {harness} session has no native session ID yet, so there is "
            "no conversation to fork. Start the session first"
        )
    return session_id


async def _read_all(
    groups: Any, rows: dict[str, dict[str, Any]], harness: str, ledger: TransferLedger
) -> list[Any]:
    reads = []
    for name, row in rows.items():
        session_id = row.get("id")
        if not isinstance(session_id, str) or not session_id:
            continue
        active = name == harness
        if not active and row.get("status") == MISSING:
            continue
        read = await _read_history(groups, name, session_id, ledger)
        if not active and read.outcome == MISSING:
            continue
        _require_complete(read, source=active)
        reads.append(read)
    return reads


async def fork_session(
    groups: Any, request: dict[str, Any], title: str, group: str
) -> dict[str, Any]:
    """Implement the ``session_fork`` supervisor operation."""
    source = groups.custom(str(request.get("terminal") or ""))
    _ensure_eligible(source)
    harness = str(source.get("harness"))
    target = request.get("harness") or harness
    if target not in HARNESSES:
        raise ValueError("Unsupported harness: " + str(target))
    check_available = getattr(groups, "harness_available", None)
    installed = available(target)
    if not installed and check_available is not None:
        installed = check_available(target)
    if not installed:
        raise ValueError("Independent session harness unavailable: " + str(target))
    _ensure_stopped(groups, source)
    session_id = _source_session(source, harness)

    rows = copy.deepcopy(native_sessions(source))
    rows[harness] = {"id": session_id, "status": AVAILABLE}
    source_ledger = _ledger(source)
    await _ensure_no_native_writer(groups, rows, harness)

    _progress(groups, "Reading conversation history")
    reads = await _read_all(groups, rows, harness, source_ledger)

    ordinals: dict[str, Any] = {
        "transfer_ordinals": copy.deepcopy(source.get("transfer_ordinals")),
        "transfer_ordinal_seq": source.get("transfer_ordinal_seq"),
    }
    ordered = _assign_ordinals(ordinals, reads)
    plan = source_ledger.fork_plan(
        [read.history() for read in ordered.values()], str(target)
    )
    reduction = request.get("reduction")
    if reduction is not None:
        full = plan
        plan = _apply_reduction(plan, reduction)
        if plan.events == () and full.events:
            raise ValueError(
                "The selected reduction removed every event; choose another "
                "transfer or cancel"
            )

    terminal_id = uuid.uuid4().hex
    _progress(groups, "Preparing the new session")
    try:
        target_id, created = await _create_target(groups, str(target), {"title": title})
    except ValueError as error:
        raise ValueError(
            f"{error}. Nothing was forked; retry when the target harness is reachable"
        ) from error

    try:
        ledger = TransferLedger()
        entry: dict[str, Any] = {
            "id": terminal_id,
            "harness": target,
            "ticket": "",
            "column": "",
            "slot": "",
            "owner": "custom",
            "native_session": target_id,
            "native_sessions": {target: {"id": target_id, "status": AVAILABLE}},
            "group": group,
            "title": title,
            "prompt": "",
            "status": "stopped",
            "rows": 24,
            "cols": 80,
            "transfer_ordinals": ordinals["transfer_ordinals"],
            "transfer_ordinal_seq": ordinals["transfer_ordinal_seq"],
        }
        transfer: dict[str, Any] = {
            "first_ordinal": plan.first_ordinal,
            "last_ordinal": plan.last_ordinal,
            "characters": plan.size.characters,
            "bytes": plan.size.bytes,
        }
        result: dict[str, Any] = {
            "terminal": terminal_id,
            "source": source["id"],
            "harness": target,
            "native_session": target_id,
            "delivered": len(plan.events),
            "import_id": None,
            "unsupported": list(plan.unsupported),
            "transfer": transfer,
        }
        if not plan.events:
            if created and target in ("claude", "pi"):
                entry["native_assigned"] = True
            _store_ledger(entry, ledger)
            groups.register_session(entry)
            groups.state.save()
            return result

        chosen_id = None
        if target == "codex":
            chosen_id = _client_message_id(plan)
        elif target == "opencode":
            chosen_id = _opencode_message_id(plan)
        context_path = _context_path(groups.state, terminal_id, str(plan.import_id))
        try:
            _write_context(context_path, plan.text)
        except OSError as error:
            _remove_context(context_path)
            raise ValueError(
                f"Could not prepare the transfer context file: {error}. "
                "Nothing was forked; retry"
            ) from error
        record = ledger.begin_delivery(plan, chosen_id)

        _progress(groups, f"Delivering the conversation to {_harness_label(target)}")
        try:
            delivery = await _deliver(
                groups, str(target), target_id, plan, context_path, created
            )
        except (_DeliveryMissing, _DeliveryUncertain, _DeliveryFailed) as error:
            _remove_context(context_path)
            raise ValueError(
                f"Delivery to {target} did not complete: {_error_text(error)}. "
                "Nothing was forked; the source session is unchanged. Retry the fork"
            ) from error
        except Exception as error:
            _remove_context(context_path)
            raise ValueError(
                f"Delivery to {target} could not be completed: {_error_text(error)}. "
                "Nothing was forked; the source session is unchanged. Retry the fork"
            ) from error

        ledger.confirm_delivery(record.import_id, delivery.target_message_id)
        _store_ledger(entry, ledger)
        groups.register_session(entry)
        groups.state.save()
        result["import_id"] = record.import_id
        result["target_message_id"] = delivery.target_message_id
        return result
    except BaseException:
        await _discard_target(groups, str(target), target_id)
        raise


async def _discard_target(groups: Any, target: str, session_id: str) -> None:
    delete = getattr(groups, "delete_native", None)
    if delete is None:
        return
    with suppress(ValueError):
        await delete({"harness": target, "native_session": session_id})

import json

import pytest

from kanbeat.session_transfer import (
    ConversationEvent,
    HarnessHistory,
    TransferLedger,
    UncertainDeliveryError,
    find_import_markers,
    marker_for,
    render_envelope,
)


def event(harness, ordinal, role="user", text=None, **kwargs):
    return ConversationEvent(
        harness,
        ordinal,
        role,
        text if text is not None else f"{harness}-{ordinal}",
        event_id=kwargs.pop("event_id", f"message-{ordinal}"),
        **kwargs,
    )


def test_codex_opencode_return_only_sends_new_original_events():
    ledger = TransferLedger()
    codex = [event("codex", i) for i in range(20)]
    outbound = ledger.plan(codex, "opencode")
    assert outbound.events == tuple(codex)
    ledger.commit_delivery(outbound, "import-codex")
    imported = event("opencode", 20, text=outbound.text, event_id="import-codex")
    assert ledger.plan([*codex, imported], "codex").is_empty
    opencode = [event("opencode", i) for i in range(21, 29)]
    inbound = ledger.plan([*codex, imported, *opencode], "codex")
    assert inbound.events == tuple(opencode)
    ledger.commit_delivery(inbound)
    assert not ledger.plan([*codex, imported, *opencode], "codex")
    assert not ledger.plan([*codex, imported, *opencode], "opencode")


def test_repeated_four_harness_cycles_preserve_exact_delivery_after_restart():
    ledger = TransferLedger()
    histories = []
    received = {harness: set() for harness in ("codex", "opencode", "claude", "pi")}
    ordinal = 0
    for target in ["codex", "opencode", "claude", "pi"] * 5:
        originals = [row for row in histories if not ledger.is_import_event(row)]
        expected = tuple(
            row
            for row in originals
            if row.harness != target and row.key not in received[target]
        )
        plan = ledger.plan(reversed(histories), target)
        assert plan.events == expected
        if plan:
            assert len(find_import_markers(plan.text)) == 1
            imported = event(target, ordinal, text=plan.text)
            ledger.commit_delivery(plan, imported.event_id)
            received[target].update(row.key for row in plan.events)
            histories.append(imported)
            ordinal += 1
        original = event(target, ordinal, role="assistant")
        histories.append(original)
        received[target].add(original.key)
        ordinal += 1
        ledger = TransferLedger.from_json(ledger.to_json())
        assert not ledger.plan(histories, target)


@pytest.mark.parametrize("recognition", ["flag", "import_id", "marker", "message_id"])
def test_import_recognition_does_not_exclude_real_replies(recognition):
    ledger = TransferLedger()
    original = event("codex", 0)
    plan = ledger.plan([original], "claude")
    ledger.commit_delivery(plan, "native-import")
    options = {
        "flag": {"is_import": True},
        "import_id": {"import_id": plan.import_id},
        "marker": {"text": plan.text},
        "message_id": {"event_id": "native-import"},
    }
    imported = event("claude", 1, **options[recognition])
    reply = event("claude", 2, "assistant", "A new reply")
    assert ledger.is_import_event(imported)
    assert ledger.plan([original, imported, reply], "pi").events == (original, reply)


def test_envelope_keeps_chronological_text_and_tool_labels():
    rows = (
        event("codex", 0, text="First line\nSecond line: 日本語"),
        event("claude", 1, "assistant", "Response"),
        event("pi", 2, "tool_call", "read file", tool_name="read"),
        event("opencode", 3, "tool_result", "File contents", tool_name="read"),
    )
    envelope = render_envelope(reversed(rows), import_id="fixture-import")
    assert envelope.text.index("User:") < envelope.text.index("Assistant:")
    assert "User: First line\nSecond line: 日本語" in envelope.text
    assert "Tool call (read): read file" in envelope.text
    assert "Tool result (read): File contents" in envelope.text
    assert "not a new instruction" in envelope.text
    assert envelope.size.characters == len(envelope.text)
    assert envelope.size.bytes == len(envelope.text.encode("utf-8"))
    (marker,) = envelope.markers
    assert marker.import_id == "fixture-import"
    assert marker.sources == ("codex", "claude", "pi", "opencode")
    assert (marker.first_ordinal, marker.last_ordinal, marker.closed) == (0, 3, True)
    with pytest.raises(ValueError, match="Import events"):
        render_envelope([event("pi", 4, text=envelope.text)])


def test_marker_parser_finds_multiple_and_short_prompt_markers():
    first = marker_for("one", ["codex"], 0, 5)
    second = render_envelope([event("pi", 6)], import_id="two").text
    markers = find_import_markers(f"Read context file. {first}\n{second}")
    assert [(row.import_id, row.closed) for row in markers] == [
        ("one", False),
        ("two", True),
    ]


def test_large_plan_reports_exact_sizes_and_explicit_reductions():
    ledger = TransferLedger()
    rows = tuple(
        event("codex", i, "tool_result" if i % 2 else "user", "界" * 1000)
        for i in range(60)
    )
    history = HarnessHistory("codex", rows, ("attachments", "hidden reasoning"))
    plan = ledger.plan([history], "pi")
    report = plan.size_report()
    assert plan.events == rows
    assert plan.unsupported == ("attachments", "hidden reasoning")
    assert report.full.characters == len(plan.text)
    assert report.full.bytes == len(plan.text.encode("utf-8"))
    assert [(choice.kind, choice.count) for choice in report.options] == [
        ("newest", 50),
        ("newest", 20),
        ("newest", 10),
        ("text_only", None),
    ]
    for choice in report.options:
        assert choice.size.bytes == len(choice.plan.text.encode("utf-8"))
        assert choice.size.bytes < report.full.bytes
    reduced = report.options[2].plan
    assert reduced.events == rows[-10:]
    ledger.commit_delivery(reduced)
    assert ledger.plan([history], "pi").events == rows[:-10]
    assert plan.events == rows
    assert json.loads(json.dumps(report.to_dict())) == report.to_dict()


@pytest.mark.parametrize("recognition", ["marker", "message_id", "import_id"])
def test_uncertain_delivery_survives_restart_and_reconciles(recognition):
    ledger = TransferLedger()
    original = event("codex", 0)
    plan = ledger.plan([original], "pi")
    ledger.begin_delivery(plan, "native-import")
    assert not ledger.delivered("pi")
    ledger = TransferLedger.from_json(ledger.to_json())
    with pytest.raises(UncertainDeliveryError):
        ledger.plan([original], "pi")
    with pytest.raises(UncertainDeliveryError):
        ledger.begin_delivery(plan)
    options = {
        "marker": {"text": plan.text},
        "message_id": {"event_id": "native-import"},
        "import_id": {"import_id": plan.import_id},
    }
    imported = event("pi", 1, **options[recognition])
    result = ledger.reconcile("pi", [imported])
    assert len(result.confirmed) == 1
    assert not result.missing
    assert ledger.delivered("pi") == frozenset([original.key])
    assert not ledger.pending_imports()
    assert not ledger.plan([original, imported], "pi")
    assert not ledger.reconcile("pi", [imported]).confirmed


def test_absent_import_allows_retry_without_checkpointing():
    ledger = TransferLedger()
    original = event("claude", 0)
    plan = ledger.plan([original], "codex")
    ledger.begin_delivery(plan)
    result = ledger.reconcile("codex", [event("pi", 1, text=plan.text)])
    assert len(result.missing) == 1
    assert not ledger.delivered("codex")
    retry = ledger.plan([original], "codex")
    assert retry.events == plan.events
    assert retry.import_id != plan.import_id
    ledger.commit_delivery(retry)
    assert not ledger.plan([original], "codex")


def test_ledger_json_round_trip_includes_all_delivery_statuses_and_positions():
    ledger = TransferLedger()
    original = event("codex", 0, event_id=None, position=0)
    ledger.commit_delivery(ledger.plan([original], "pi"), "confirmed-id")
    ledger.begin_delivery(ledger.plan([original], "claude"), "pending-id")
    ledger.begin_delivery(ledger.plan([original], "opencode"))
    ledger.reconcile("opencode", [])
    plain = json.loads(json.dumps(ledger.to_dict()))
    restored = TransferLedger.from_dict(plain)
    assert restored == ledger
    assert restored.to_dict() == plain
    assert TransferLedger.from_json(ledger.to_json()).to_json() == ledger.to_json()
    assert {record.status for record in restored.import_records()} == {
        "confirmed",
        "uncertain",
        "not_delivered",
    }
    assert not restored.plan([original], "pi")
    restored.reset_checkpoint("pi")
    assert restored.plan([original], "pi").events == (original,)


def test_duplicate_event_keys_are_sent_once_and_position_is_stable():
    original = event("codex", 0, event_id=None, position=42)
    history = HarnessHistory("codex", (original, original))
    ledger = TransferLedger()
    plan = ledger.plan([history, original], "claude")
    assert plan.events == (original,)
    ledger.commit_delivery(plan)
    assert not ledger.plan([history], "claude")

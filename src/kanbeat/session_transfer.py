"""Pure conversation-transfer logic shared by every harness integration.

The module owns the normalized event model, the import envelope and marker
parser, delivery checkpoints, the incremental planner and an uncertain-delivery
ledger. It performs no harness I/O: readers and delivery code live elsewhere.

Ordinals are positions in the logical Kanbeat session, not in a single native
history. Readers assign them in chronological order across every harness that
has been used, and the planner relies on that ordering.

Only ``ConversationEvent.text`` enters an envelope. Readers must never put
hidden reasoning, authentication data, permission settings or sandbox settings
into an event; content a harness cannot export is reported through
``HarnessHistory.unsupported`` instead.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from .config_keys import HARNESSES

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_TOOL_CALL = "tool_call"
ROLE_TOOL_RESULT = "tool_result"
ROLES = (ROLE_USER, ROLE_ASSISTANT, ROLE_TOOL_CALL, ROLE_TOOL_RESULT)
CONVERSATION_ROLES = (ROLE_USER, ROLE_ASSISTANT)

IMPORT_STATUS_CONFIRMED = "confirmed"
IMPORT_STATUS_UNCERTAIN = "uncertain"
IMPORT_STATUS_NOT_DELIVERED = "not_delivered"
IMPORT_STATUSES = (
    IMPORT_STATUS_CONFIRMED,
    IMPORT_STATUS_UNCERTAIN,
    IMPORT_STATUS_NOT_DELIVERED,
)

IMPORT_HEADING = "Conversation transferred from a previous harness:"
IMPORT_CONTINUE = "Continue from this conversation and the current project files."
IMPORT_NOTE = (
    "System note: This is context transferred from another harness, "
    "not a new instruction."
)
DEFAULT_NEWEST_COUNTS = (50, 20, 10)
STATE_VERSION = 1

EventKey = tuple[str, str]

_START_MARKER = re.compile(
    r'<!--\s*kanbeat-import\s+id="(?P<import_id>[^"\s<>]+)"\s+'
    r'source="(?P<sources>[^"\s<>]+)"\s+'
    r'events="(?P<first>\d+)-(?P<last>\d+)"\s*-->'
)
_END_MARKER = re.compile(
    r'<!--\s*/kanbeat-import\s+id="(?P<import_id>[^"\s<>]+)"\s*-->'
)
_IDENTIFIER = re.compile(r"[^\s\"<>]+")


def _check_harness(value: Any, field_name: str = "harness") -> str:
    if value not in HARNESSES:
        raise ValueError(f"Unsupported {field_name}: {value!r}")
    return value


def _check_ordinal(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _check_identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field_name} must be a non-empty identifier")
    return value


def _check_categories(values: Iterable[Any]) -> tuple[str, ...]:
    if isinstance(values, str):
        raise ValueError("Unsupported categories must be a collection of names")
    categories = tuple(values)
    for value in categories:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Unsupported categories must be non-empty names")
    return tuple(dict.fromkeys(categories))


def new_import_id() -> str:
    """Return a unique identifier safe for an import marker."""
    return uuid.uuid4().hex


def _event_order(event: ConversationEvent) -> tuple[int, str, int, str]:
    position = event.position if event.position is not None else -1
    return (event.ordinal, event.harness, position, event.event_id or "")


@dataclass(frozen=True)
class ConversationEvent:
    """One normalized item from a native harness history.

    ``event_id`` is the harness's stable identifier when it has one. If it does
    not, ``position`` is the reliable source position. At least one is required.
    ``is_import`` marks text that was itself transferred from another harness.
    """

    harness: str
    ordinal: int
    role: str
    text: str
    event_id: str | None = None
    position: int | None = None
    is_import: bool = False
    import_id: str | None = None
    tool_name: str | None = None

    def __post_init__(self) -> None:
        _check_harness(self.harness)
        _check_ordinal(self.ordinal, "ordinal")
        if self.role not in ROLES:
            raise ValueError(f"Unsupported event role: {self.role!r}")
        if not isinstance(self.text, str):
            raise ValueError("Event text must be a string")
        if self.event_id is not None:
            _check_identifier(self.event_id, "event_id")
        if self.position is not None:
            _check_ordinal(self.position, "position")
        if self.event_id is None and self.position is None:
            raise ValueError("An event needs an event_id or a position")
        if self.import_id is not None:
            _check_identifier(self.import_id, "import_id")
            if not self.is_import:
                object.__setattr__(self, "is_import", True)
        if self.tool_name is not None:
            _check_identifier(self.tool_name, "tool_name")

    @property
    def key(self) -> EventKey:
        if self.event_id is not None:
            return (self.harness, f"id:{self.event_id}")
        return (self.harness, f"pos:{self.position}")

    @property
    def label(self) -> str:
        if self.role == ROLE_USER:
            return "User"
        if self.role == ROLE_ASSISTANT:
            return "Assistant"
        base = "Tool call" if self.role == ROLE_TOOL_CALL else "Tool result"
        return f"{base} ({self.tool_name})" if self.tool_name else base

    def render(self) -> str:
        """Render the event as labelled conversation text."""
        return f"{self.label}: {self.text}"


@dataclass(frozen=True)
class HarnessHistory:
    """Events read from one harness, plus content it cannot export."""

    harness: str
    events: tuple[ConversationEvent, ...] = ()
    unsupported: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _check_harness(self.harness)
        events = tuple(self.events)
        for event in events:
            if not isinstance(event, ConversationEvent):
                raise ValueError("History events must be ConversationEvent objects")
            if event.harness != self.harness:
                raise ValueError("History contains an event from another harness")
        object.__setattr__(self, "events", events)
        object.__setattr__(self, "unsupported", _check_categories(self.unsupported))


@dataclass(frozen=True)
class TextSize:
    characters: int
    bytes: int

    def to_dict(self) -> dict[str, int]:
        return {"characters": self.characters, "bytes": self.bytes}


@dataclass(frozen=True)
class ImportMarker:
    import_id: str
    sources: tuple[str, ...]
    first_ordinal: int
    last_ordinal: int
    closed: bool = False

    @property
    def source(self) -> str:
        return "+".join(self.sources)


def find_import_markers(text: str) -> tuple[ImportMarker, ...]:
    """Find every import start marker in text, including incomplete envelopes."""
    if not isinstance(text, str):
        return ()
    closed = {match.group("import_id") for match in _END_MARKER.finditer(text)}
    markers = []
    for match in _START_MARKER.finditer(text):
        sources = tuple(part for part in match.group("sources").split("+") if part)
        if not sources:
            continue
        markers.append(
            ImportMarker(
                import_id=match.group("import_id"),
                sources=sources,
                first_ordinal=int(match.group("first")),
                last_ordinal=int(match.group("last")),
                closed=match.group("import_id") in closed,
            )
        )
    return tuple(markers)


def has_import_marker(text: str) -> bool:
    """Whether text contains at least one import start marker."""
    return isinstance(text, str) and _START_MARKER.search(text) is not None


def _marker_line(
    import_id: str, sources: Sequence[str], first_ordinal: int, last_ordinal: int
) -> str:
    _check_identifier(import_id, "import_id")
    names = tuple(_check_harness(source, "source harness") for source in sources)
    if not names:
        raise ValueError("An import marker needs at least one source harness")
    _check_ordinal(first_ordinal, "first ordinal")
    _check_ordinal(last_ordinal, "last ordinal")
    if first_ordinal > last_ordinal:
        raise ValueError("An import range cannot end before it starts")
    source = "+".join(dict.fromkeys(names))
    return (
        f'<!-- kanbeat-import id="{import_id}" source="{source}" '
        f'events="{first_ordinal}-{last_ordinal}" -->'
    )


def marker_for(
    import_id: str, sources: Sequence[str], first_ordinal: int, last_ordinal: int
) -> str:
    """Render a raw start marker.

    A bare marker quoted inside a conversation is not an import by itself. Use
    ``render_short_prompt`` for a delivery prompt that later reads must
    recognize.
    """
    return _marker_line(import_id, sources, first_ordinal, last_ordinal)


def _end_marker_line(import_id: str) -> str:
    return f'<!-- /kanbeat-import id="{import_id}" -->'


def render_short_prompt(
    instruction: str,
    import_id: str,
    sources: Sequence[str],
    first_ordinal: int,
    last_ordinal: int,
) -> str:
    """Render a short delivery prompt wrapped in a complete import marker."""
    if not isinstance(instruction, str) or not instruction.strip():
        raise ValueError("A delivery prompt needs instruction text")
    start = _marker_line(import_id, sources, first_ordinal, last_ordinal)
    return f"{start}\n{instruction.strip()}\n{_end_marker_line(import_id)}"


def import_id_for_text(text: str) -> str | None:
    """Return the import id when text is exactly one complete import envelope."""
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    start = _START_MARKER.match(stripped)
    if start is None:
        return None
    identifier = start.group("import_id")
    ends = [
        marker
        for marker in _END_MARKER.finditer(stripped)
        if marker.group("import_id") == identifier
    ]
    if not ends or ends[-1].end() != len(stripped):
        return None
    return identifier


def _marker_import_id(event: ConversationEvent) -> str | None:
    if event.role != ROLE_USER:
        return None
    return import_id_for_text(event.text)


def _marker_text_is_import(event: ConversationEvent) -> bool:
    return _marker_import_id(event) is not None


def _looks_like_import(event: ConversationEvent) -> bool:
    return bool(event.is_import or event.import_id or _marker_text_is_import(event))


@dataclass(frozen=True)
class TransferEnvelope:
    import_id: str
    sources: tuple[str, ...]
    first_ordinal: int
    last_ordinal: int
    text: str

    @property
    def size(self) -> TextSize:
        return TextSize(len(self.text), len(self.text.encode("utf-8")))

    @property
    def markers(self) -> tuple[ImportMarker, ...]:
        return find_import_markers(self.text)


def render_envelope(
    events: Iterable[ConversationEvent], *, import_id: str | None = None
) -> TransferEnvelope:
    """Render a complete import envelope for original conversation events.

    Import events are rejected so an earlier envelope can never be nested in a
    later one. Nothing is truncated or summarized here.
    """
    rows = tuple(events)
    if not rows:
        raise ValueError("Cannot render an envelope without events")
    seen: set[EventKey] = set()
    for event in rows:
        if not isinstance(event, ConversationEvent):
            raise ValueError("Envelope events must be ConversationEvent objects")
        if event.key in seen:
            raise ValueError("Duplicate conversation event in envelope")
        seen.add(event.key)
        if _looks_like_import(event):
            raise ValueError("Import events cannot be embedded in a new envelope")
    ordered = tuple(sorted(rows, key=_event_order))

    identifier = new_import_id() if import_id is None else import_id
    sources = tuple(dict.fromkeys(event.harness for event in ordered))
    first_ordinal = ordered[0].ordinal
    last_ordinal = ordered[-1].ordinal
    lines = [
        _marker_line(identifier, sources, first_ordinal, last_ordinal),
        IMPORT_HEADING,
        "",
    ]
    for event in ordered:
        lines.append(event.render())
        lines.append("")
    lines.extend([IMPORT_CONTINUE, "", IMPORT_NOTE, _end_marker_line(identifier)])
    return TransferEnvelope(
        import_id=identifier,
        sources=sources,
        first_ordinal=first_ordinal,
        last_ordinal=last_ordinal,
        text="\n".join(lines),
    )


@dataclass(frozen=True)
class ReductionOption:
    """One explicit reduced transfer offered beside the full plan."""

    kind: str
    count: int | None
    description: str
    plan: TransferPlan

    @property
    def size(self) -> TextSize:
        return self.plan.size

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "count": self.count,
            "description": self.description,
            "size": self.size.to_dict(),
        }


@dataclass(frozen=True)
class TransferSizeReport:
    """Exact size of a full plan and its explicit reduced alternatives."""

    full: TextSize
    options: tuple[ReductionOption, ...]

    @property
    def has_reductions(self) -> bool:
        return bool(self.options)

    def to_dict(self) -> dict[str, Any]:
        return {
            "full": self.full.to_dict(),
            "options": [option.to_dict() for option in self.options],
        }


@dataclass(frozen=True)
class TransferPlan:
    """The exact conversation events a target harness has not received."""

    target: str
    events: tuple[ConversationEvent, ...] = ()
    unsupported: tuple[str, ...] = ()
    import_id: str | None = None

    def __post_init__(self) -> None:
        _check_harness(self.target, "target harness")
        events = tuple(self.events)
        for event in events:
            if not isinstance(event, ConversationEvent):
                raise ValueError("Plan events must be ConversationEvent objects")
            if event.harness == self.target:
                raise ValueError("A plan cannot contain events from its target")
            if _looks_like_import(event):
                raise ValueError("A plan cannot contain import events")
        unique: dict[EventKey, ConversationEvent] = {}
        for event in events:
            unique.setdefault(event.key, event)
        ordered = tuple(sorted(unique.values(), key=_event_order))
        unsupported = _check_categories(self.unsupported)
        identifier = self.import_id
        if ordered:
            identifier = new_import_id() if identifier is None else identifier
            _check_identifier(identifier, "import_id")
        elif identifier is not None:
            raise ValueError("An empty plan cannot have an import id")
        object.__setattr__(self, "events", ordered)
        object.__setattr__(self, "unsupported", unsupported)
        object.__setattr__(self, "import_id", identifier)

    def __bool__(self) -> bool:
        return bool(self.events)

    def __len__(self) -> int:
        return len(self.events)

    @property
    def is_empty(self) -> bool:
        return not self.events

    @property
    def envelope(self) -> TransferEnvelope | None:
        if not self.events or self.import_id is None:
            return None
        return render_envelope(self.events, import_id=self.import_id)

    @property
    def text(self) -> str:
        envelope = self.envelope
        return "" if envelope is None else envelope.text

    @property
    def size(self) -> TextSize:
        envelope = self.envelope
        return TextSize(0, 0) if envelope is None else envelope.size

    @property
    def size_characters(self) -> int:
        return self.size.characters

    @property
    def size_bytes(self) -> int:
        return self.size.bytes

    @property
    def sources(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(event.harness for event in self.events))

    @property
    def first_ordinal(self) -> int | None:
        return self.events[0].ordinal if self.events else None

    @property
    def last_ordinal(self) -> int | None:
        return self.events[-1].ordinal if self.events else None

    def reduce_to_newest(self, count: int) -> TransferPlan:
        """Return a plan with only the newest ``count`` events, explicitly."""
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError("count must be a positive integer")
        if count >= len(self.events):
            return self
        return TransferPlan(self.target, self.events[-count:], self.unsupported)

    def reduce_to_roles(
        self, roles: Iterable[str] = CONVERSATION_ROLES
    ) -> TransferPlan:
        """Return a plan with only events in the given roles, explicitly."""
        selected = tuple(dict.fromkeys(roles))
        if not selected:
            raise ValueError("A reduced plan needs at least one role")
        for role in selected:
            if role not in ROLES:
                raise ValueError(f"Unsupported event role: {role!r}")
        events = tuple(event for event in self.events if event.role in selected)
        if len(events) == len(self.events):
            return self
        return TransferPlan(self.target, events, self.unsupported)

    def reduction_options(
        self, newest_counts: Sequence[int] = DEFAULT_NEWEST_COUNTS
    ) -> tuple[ReductionOption, ...]:
        """Offer explicit smaller transfers without changing this plan."""
        if not self.events:
            return ()
        options: list[ReductionOption] = []
        seen: set[int] = set()
        for count in newest_counts:
            if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                raise ValueError("newest counts must be positive integers")
            if count in seen or count >= len(self.events):
                continue
            seen.add(count)
            options.append(
                ReductionOption(
                    kind="newest",
                    count=count,
                    description=f"Newest {count} events",
                    plan=self.reduce_to_newest(count),
                )
            )
        text_plan = self.reduce_to_roles(CONVERSATION_ROLES)
        if text_plan.events and text_plan.events != self.events:
            options.append(
                ReductionOption(
                    kind="text_only",
                    count=None,
                    description="User and assistant text only",
                    plan=text_plan,
                )
            )
        return tuple(options)

    def size_report(
        self, newest_counts: Sequence[int] = DEFAULT_NEWEST_COUNTS
    ) -> TransferSizeReport:
        """Report the exact full size and the exact size of each reduction."""
        return TransferSizeReport(self.size, self.reduction_options(newest_counts))


@dataclass(frozen=True)
class ImportRecord:
    """Provenance for one import delivered to one target harness."""

    import_id: str
    target: str
    sources: tuple[str, ...]
    first_ordinal: int
    last_ordinal: int
    event_keys: tuple[EventKey, ...]
    status: str = IMPORT_STATUS_CONFIRMED
    target_message_id: str | None = None

    def __post_init__(self) -> None:
        _check_identifier(self.import_id, "import_id")
        _check_harness(self.target, "target harness")
        sources = tuple(self.sources)
        if not sources:
            raise ValueError("An import record needs at least one source harness")
        for source in sources:
            _check_harness(source, "source harness")
        _check_ordinal(self.first_ordinal, "first ordinal")
        _check_ordinal(self.last_ordinal, "last ordinal")
        if self.first_ordinal > self.last_ordinal:
            raise ValueError("An import range cannot end before it starts")
        keys = tuple(self.event_keys)
        for key in keys:
            if (
                not isinstance(key, tuple)
                or len(key) != 2
                or not all(isinstance(part, str) and part for part in key)
            ):
                raise ValueError("Import event keys must be (harness, key) pairs")
            _check_harness(key[0], "event harness")
        if self.status not in IMPORT_STATUSES:
            raise ValueError(f"Unsupported import status: {self.status!r}")
        if self.target_message_id is not None:
            _check_identifier(self.target_message_id, "target_message_id")
        object.__setattr__(self, "sources", tuple(dict.fromkeys(sources)))
        object.__setattr__(self, "event_keys", keys)

    def to_dict(self) -> dict[str, Any]:
        return {
            "import_id": self.import_id,
            "target": self.target,
            "sources": list(self.sources),
            "first_ordinal": self.first_ordinal,
            "last_ordinal": self.last_ordinal,
            "event_keys": [[harness, key] for harness, key in self.event_keys],
            "status": self.status,
            "target_message_id": self.target_message_id,
        }

    @classmethod
    def from_dict(cls, value: Any) -> ImportRecord:
        if not isinstance(value, dict):
            raise ValueError("Import record must be an object")
        keys_value = value.get("event_keys")
        if not isinstance(keys_value, list):
            raise ValueError("Import record event_keys must be a list")
        keys = []
        for row in keys_value:
            if isinstance(row, (list, tuple)) and len(row) == 2:
                keys.append((row[0], row[1]))
            elif isinstance(row, dict):
                keys.append((row.get("harness"), row.get("key")))
            else:
                raise ValueError("Invalid import event key")
        sources_value = value.get("sources")
        if not isinstance(sources_value, (list, tuple)):
            raise ValueError("Import record sources must be a list")
        return cls(
            import_id=value.get("import_id"),
            target=value.get("target"),
            sources=tuple(sources_value),
            first_ordinal=value.get("first_ordinal"),
            last_ordinal=value.get("last_ordinal"),
            event_keys=tuple(keys),
            status=value.get("status", IMPORT_STATUS_CONFIRMED),
            target_message_id=value.get("target_message_id"),
        )


@dataclass(frozen=True)
class ReconcileResult:
    """Outcome of checking uncertain imports against a target's history."""

    target: str
    confirmed: tuple[ImportRecord, ...] = ()
    missing: tuple[ImportRecord, ...] = ()
    uncertain: tuple[ImportRecord, ...] = ()


class UncertainDeliveryError(RuntimeError):
    """Raised when planning while an earlier delivery is still uncertain."""

    def __init__(self, target: str, import_ids: Sequence[str]):
        self.target = target
        self.import_ids = tuple(import_ids)
        joined = ", ".join(self.import_ids)
        super().__init__(
            f"Delivery to {target} is uncertain for import(s) {joined}; "
            "reconcile them against the target history before retrying"
        )


def _coerce_key(value: Any) -> EventKey:
    if isinstance(value, dict):
        harness, key = value.get("harness"), value.get("key")
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        harness, key = value
    else:
        raise ValueError("Event keys must be (harness, key) pairs")
    _check_harness(harness, "event harness")
    if not isinstance(key, str) or not key:
        raise ValueError("Event key must be a non-empty string")
    return (harness, key)


def _key_dict(key: EventKey) -> dict[str, str]:
    return {"harness": key[0], "key": key[1]}


@dataclass
class TransferLedger:
    """Per-session checkpoints and import provenance, serializable as JSON."""

    checkpoints: dict[str, set[EventKey]] = field(default_factory=dict)
    imports: dict[str, ImportRecord] = field(default_factory=dict)

    def __post_init__(self) -> None:
        normalized: dict[str, set[EventKey]] = {}
        for target, keys in self.checkpoints.items():
            _check_harness(target, "target harness")
            normalized[target] = {_coerce_key(key) for key in keys}
        self.checkpoints = normalized
        for import_id, record in self.imports.items():
            if not isinstance(record, ImportRecord):
                raise ValueError("Imports must be ImportRecord objects")
            if record.import_id != import_id:
                raise ValueError("Import registry key does not match its record")
        self.imports = dict(self.imports)

    def delivered(self, target: str) -> frozenset[EventKey]:
        """Source events recorded as delivered to a target harness."""
        _check_harness(target, "target harness")
        return frozenset(self.checkpoints.get(target, ()))

    def mark_delivered(self, target: str, events: Iterable[ConversationEvent]) -> None:
        """Record events as delivered to a target without an import record."""
        _check_harness(target, "target harness")
        rows = tuple(events)
        for event in rows:
            if not isinstance(event, ConversationEvent):
                raise ValueError("Delivered events must be ConversationEvent objects")
        self.checkpoints.setdefault(target, set()).update(event.key for event in rows)

    def reset_checkpoint(self, target: str) -> None:
        """Forget delivered source events, for example after a fresh session."""
        _check_harness(target, "target harness")
        self.checkpoints.pop(target, None)

    def import_records(self, target: str | None = None) -> tuple[ImportRecord, ...]:
        records = sorted(self.imports.values(), key=lambda row: row.import_id)
        if target is None:
            return tuple(records)
        _check_harness(target, "target harness")
        return tuple(record for record in records if record.target == target)

    def pending_imports(self, target: str | None = None) -> tuple[ImportRecord, ...]:
        """Imports whose delivery started but has not been confirmed."""
        records = self.import_records(target)
        return tuple(
            record for record in records if record.status == IMPORT_STATUS_UNCERTAIN
        )

    def is_import_event(self, event: ConversationEvent) -> bool:
        """Recognize an import by flag, marker or recorded target message id."""
        if _looks_like_import(event):
            return True
        if event.event_id is None:
            return False
        return any(
            record.target == event.harness
            and record.target_message_id == event.event_id
            for record in self.imports.values()
        )

    def plan(
        self,
        histories: Iterable[HarnessHistory | ConversationEvent],
        target: str,
    ) -> TransferPlan:
        """Plan exactly the original events the target has not received."""
        _check_harness(target, "target harness")
        uncertain = self.pending_imports(target)
        if uncertain:
            raise UncertainDeliveryError(
                target, [record.import_id for record in uncertain]
            )
        events, unsupported = self._collect(histories, target)
        imports = {event.key for event in events if self.is_import_event(event)}
        delivered = self.checkpoints.get(target, set())
        unique: dict[EventKey, ConversationEvent] = {}
        for event in events:
            if event.key in imports:
                continue
            if event.harness == target:
                continue
            if event.key in delivered:
                continue
            unique.setdefault(event.key, event)
        return TransferPlan(
            target=target,
            events=tuple(unique.values()),
            unsupported=unsupported,
        )

    def begin_delivery(
        self, plan: TransferPlan, target_message_id: str | None = None
    ) -> ImportRecord:
        """Record that delivery started, without claiming it succeeded.

        ``target_message_id`` may be supplied when the caller can choose the
        target message id before sending. It is only a reconciliation aid; the
        source events remain uncheckpointed until the delivery is confirmed.
        """
        if not isinstance(plan, TransferPlan):
            raise ValueError("Delivery requires a TransferPlan")
        if not plan.events or plan.import_id is None:
            raise ValueError("Nothing to deliver")
        if target_message_id is not None:
            _check_identifier(target_message_id, "target_message_id")
        uncertain = self.pending_imports(plan.target)
        if uncertain:
            raise UncertainDeliveryError(
                plan.target, [record.import_id for record in uncertain]
            )
        if plan.import_id in self.imports:
            raise ValueError(f"Import {plan.import_id} is already recorded")
        delivered = self.checkpoints.get(plan.target, set())
        repeated = [event.key for event in plan.events if event.key in delivered]
        if repeated:
            raise ValueError("Plan contains events already delivered to the target")
        record = ImportRecord(
            import_id=plan.import_id,
            target=plan.target,
            sources=plan.sources,
            first_ordinal=plan.events[0].ordinal,
            last_ordinal=plan.events[-1].ordinal,
            event_keys=tuple(event.key for event in plan.events),
            status=IMPORT_STATUS_UNCERTAIN,
            target_message_id=target_message_id,
        )
        self.imports[record.import_id] = record
        return record

    def confirm_delivery(
        self, import_id: str, target_message_id: str | None = None
    ) -> ImportRecord:
        """Confirm an import and checkpoint its source events for the target."""
        record = self.imports.get(import_id)
        if record is None:
            raise ValueError(f"Unknown import: {import_id}")
        if target_message_id is not None:
            _check_identifier(target_message_id, "target_message_id")
        identifier = target_message_id or record.target_message_id
        if (
            record.status != IMPORT_STATUS_CONFIRMED
            or identifier != record.target_message_id
        ):
            record = replace(
                record,
                status=IMPORT_STATUS_CONFIRMED,
                target_message_id=identifier,
            )
            self.imports[import_id] = record
        self.checkpoints.setdefault(record.target, set()).update(record.event_keys)
        return record

    def commit_delivery(
        self, plan: TransferPlan, target_message_id: str | None = None
    ) -> ImportRecord:
        """Record a delivery that is confirmed as soon as it returns."""
        record = self.begin_delivery(plan)
        return self.confirm_delivery(record.import_id, target_message_id)

    def mark_not_delivered(self, import_id: str) -> ImportRecord:
        """Resolve an uncertain import as absent so it may be planned again."""
        record = self.imports.get(import_id)
        if record is None:
            raise ValueError(f"Unknown import: {import_id}")
        if record.status == IMPORT_STATUS_CONFIRMED:
            raise ValueError("A confirmed import cannot be marked not delivered")
        if record.status == IMPORT_STATUS_NOT_DELIVERED:
            return record
        record = replace(record, status=IMPORT_STATUS_NOT_DELIVERED)
        self.imports[import_id] = record
        return record

    def reconcile(
        self,
        target: str,
        events: Iterable[ConversationEvent],
        *,
        history_complete: bool = False,
    ) -> ReconcileResult:
        """Resolve uncertain imports by looking for their marker in target history.

        A match always confirms an import. Absence is only proof of failure when
        ``history_complete`` is true and comes from a successful, complete read
        of the target history. With the default ``False``, failed or partial
        reads leave the delivery uncertain and planning stays blocked.
        """
        _check_harness(target, "target harness")
        if not isinstance(history_complete, bool):
            raise ValueError("history_complete must be a boolean")
        history = tuple(events)
        for event in history:
            if not isinstance(event, ConversationEvent):
                raise ValueError("Reconciliation needs ConversationEvent objects")
        confirmed: list[ImportRecord] = []
        missing: list[ImportRecord] = []
        unresolved: list[ImportRecord] = []
        pending = self.pending_imports(target)
        for record in pending:
            match = self._find_import(record, history)
            if match is None:
                if not history_complete:
                    unresolved.append(record)
                    continue
                updated = replace(record, status=IMPORT_STATUS_NOT_DELIVERED)
                self.imports[record.import_id] = updated
                missing.append(updated)
                continue
            updated = replace(
                record,
                status=IMPORT_STATUS_CONFIRMED,
                target_message_id=match.event_id or record.target_message_id,
            )
            self.imports[record.import_id] = updated
            self.checkpoints.setdefault(target, set()).update(updated.event_keys)
            confirmed.append(updated)
        return ReconcileResult(
            target=target,
            confirmed=tuple(confirmed),
            missing=tuple(missing),
            uncertain=tuple(unresolved),
        )

    def _find_import(
        self, record: ImportRecord, history: Sequence[ConversationEvent]
    ) -> ConversationEvent | None:
        for event in history:
            if event.harness != record.target:
                continue
            if (
                record.target_message_id is not None
                and event.event_id == record.target_message_id
            ):
                return event
            if event.import_id == record.import_id:
                return event
            if _marker_import_id(event) == record.import_id:
                return event
        return None

    @staticmethod
    def _collect(
        histories: Iterable[HarnessHistory | ConversationEvent], target: str
    ) -> tuple[tuple[ConversationEvent, ...], tuple[str, ...]]:
        if isinstance(histories, (HarnessHistory, ConversationEvent)):
            items: tuple[HarnessHistory | ConversationEvent, ...] = (histories,)
        else:
            try:
                items = tuple(histories)
            except TypeError as exc:
                raise TypeError("Expected histories or events") from exc
        events: list[ConversationEvent] = []
        unsupported: list[str] = []
        for item in items:
            if isinstance(item, HarnessHistory):
                events.extend(item.events)
                if item.harness != target:
                    unsupported.extend(item.unsupported)
            elif isinstance(item, ConversationEvent):
                events.append(item)
            else:
                raise TypeError("Expected HarnessHistory or ConversationEvent")
        categories = _check_categories(unsupported)
        return tuple(events), tuple(sorted(categories))

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "delivered": {
                target: [_key_dict(key) for key in sorted(keys)]
                for target, keys in sorted(self.checkpoints.items())
            },
            "imports": [
                self.imports[import_id].to_dict() for import_id in sorted(self.imports)
            ],
        }

    @classmethod
    def from_dict(cls, value: Any) -> TransferLedger:
        if not isinstance(value, dict):
            raise ValueError("Transfer ledger must be an object")
        version = value.get("version", STATE_VERSION)
        if version != STATE_VERSION:
            raise ValueError(f"Unsupported transfer ledger version: {version!r}")
        delivered_value = value.get("delivered", {})
        if not isinstance(delivered_value, dict):
            raise ValueError("Transfer ledger delivered must be an object")
        checkpoints: dict[str, set[EventKey]] = {}
        for target, rows in delivered_value.items():
            _check_harness(target, "target harness")
            if not isinstance(rows, (list, tuple)):
                raise ValueError("Transfer ledger checkpoints must be lists")
            checkpoints[target] = {_coerce_key(row) for row in rows}
        imports_value = value.get("imports", [])
        if not isinstance(imports_value, (list, tuple)):
            raise ValueError("Transfer ledger imports must be a list")
        imports: dict[str, ImportRecord] = {}
        for row in imports_value:
            record = ImportRecord.from_dict(row)
            if record.import_id in imports:
                raise ValueError(f"Duplicate import: {record.import_id}")
            imports[record.import_id] = record
        return cls(checkpoints=checkpoints, imports=imports)

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, text: str) -> TransferLedger:
        try:
            value = json.loads(text)
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError(f"Invalid transfer ledger JSON: {exc}") from exc
        return cls.from_dict(value)

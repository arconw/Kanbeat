"""Validated model, provider and effort settings shared between native harnesses."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

HARNESSES = ("codex", "claude", "opencode", "pi")
MAX_VALUE_LENGTH = 200
VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+=\[\]~-]*")

EFFORTS: dict[str, frozenset[str]] = {
    "codex": frozenset({"minimal", "low", "medium", "high", "xhigh"}),
    "claude": frozenset({"low", "medium", "high", "xhigh", "max"}),
    "opencode": frozenset(),
    "pi": frozenset({"off", "minimal", "low", "medium", "high", "xhigh"}),
}
PROVIDER_FIELDS = frozenset({"codex", "opencode", "pi"})
SETTINGS_FIELDS = ("model", "provider", "effort")


class SettingsError(ValueError):
    pass


def scalar(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SettingsError(f"{label} must be text, got {type(value).__name__}")
    if not value:
        return None
    if len(value) > MAX_VALUE_LENGTH:
        raise SettingsError(f"{label} is longer than {MAX_VALUE_LENGTH} characters")
    if not VALUE.fullmatch(value):
        raise SettingsError(
            f"{label} must start with a letter or digit and contain no whitespace "
            "or control characters"
        )
    return value


@dataclass(frozen=True)
class NativeSettings:
    harness: str
    model: str | None = None
    provider: str | None = None
    effort: str | None = None

    def __post_init__(self) -> None:
        if self.harness not in HARNESSES:
            raise SettingsError(f"Unsupported harness: {self.harness!r}")
        for field in SETTINGS_FIELDS:
            object.__setattr__(self, field, scalar(getattr(self, field), field))
        if self.provider and self.harness not in PROVIDER_FIELDS:
            raise SettingsError(f"{self.harness} has no provider setting")
        allowed = EFFORTS[self.harness]
        if self.effort and allowed and self.effort not in allowed:
            choices = ", ".join(sorted(allowed))
            raise SettingsError(
                f"Effort {self.effort!r} is not supported by {self.harness}; "
                f"use one of: {choices}"
            )
        if self.harness == "opencode" and bool(self.model) != bool(self.provider):
            raise SettingsError("opencode needs provider and model together")

    @property
    def empty(self) -> bool:
        return not (self.model or self.provider or self.effort)

    def to_dict(self) -> dict[str, str]:
        return {key: value for key in SETTINGS_FIELDS if (value := getattr(self, key))}

    @classmethod
    def from_dict(cls, harness: str, data: Any) -> "NativeSettings":
        if data is None or data == {}:
            return cls(harness)
        if not isinstance(data, dict):
            raise SettingsError("Native settings must be an object")
        unknown = set(data) - set(SETTINGS_FIELDS)
        if unknown:
            raise SettingsError(
                "Unknown native settings: " + ", ".join(sorted(unknown))
            )
        return cls(harness, data.get("model"), data.get("provider"), data.get("effort"))


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _lenient(harness: str, model: Any, provider: Any, effort: Any) -> NativeSettings:
    return NativeSettings(
        harness,
        scalar(model, "model"),
        scalar(provider, "provider"),
        scalar(effort, "effort"),
    )


def from_codex_thread(thread: Any) -> NativeSettings:
    data = _object(thread)
    return _lenient(
        "codex",
        data.get("model"),
        data.get("modelProvider"),
        data.get("reasoningEffort"),
    )


def from_claude_statusline(payload: Any) -> NativeSettings:
    data = _object(payload)
    model = data.get("model")
    model_id = model.get("id") if isinstance(model, dict) else model
    effort = _object(data.get("effort")).get("level")
    return _lenient("claude", model_id, None, effort)


def from_opencode_session(session: Any) -> NativeSettings:
    data = _object(session)
    ref = _object(data.get("model"))
    model = ref.get("modelID") or ref.get("id")
    return _lenient("opencode", model, ref.get("providerID"), ref.get("variant"))


def native_id(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if (
        not isinstance(value, str)
        or len(value) > 512
        or value.startswith("-")
        or any(c.isspace() or ord(c) < 32 for c in value)
    ):
        raise SettingsError("Invalid native session identifier")
    return value


def cli_args(
    harness: str, settings: NativeSettings | None, *, interactive: bool = False
) -> list[str]:
    if harness not in HARNESSES:
        raise SettingsError("Unsupported harness: " + harness)
    if settings is None or settings.empty:
        return []
    if settings.harness != harness:
        raise SettingsError(
            f"Settings for {settings.harness} cannot be applied to {harness}"
        )
    if harness == "codex":
        out: list[str] = []
        if settings.provider:
            out += ["-c", f'model_provider="{settings.provider}"']
        if settings.model:
            out += ["-m", settings.model]
        if settings.effort:
            out += ["-c", f'model_reasoning_effort="{settings.effort}"']
        return out
    if harness == "claude":
        out = []
        if settings.model:
            out += ["--model", settings.model]
        if settings.effort:
            out += ["--effort", settings.effort]
        return out
    if harness == "opencode":
        out = []
        if settings.model and settings.provider:
            out += ["--model", f"{settings.provider}/{settings.model}"]
        if settings.effort and not interactive:
            out += ["--variant", settings.effort]
        return out
    out = []
    if settings.provider:
        out += ["--provider", settings.provider]
    if settings.model:
        out += ["--model", settings.model]
    if settings.effort:
        out += ["--thinking", settings.effort]
    return out

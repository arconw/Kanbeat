from __future__ import annotations

from typing import Any

HARNESSES = ("codex", "claude", "opencode", "pi")
TEXT_LIMIT = 20000
TEXT_LIMITS = {"transition_hook.prompt": 8000}
MAX_PARALLEL_LIMIT = 32
TEXT_KEYS = (
    "global_cp",
    "developer_prompt",
    "reviewer_prompt",
    "transition_hook.prompt",
)
CONFIG_KEYS = (
    "default_harness",
    "max_parallel",
    *TEXT_KEYS[:3],
    "transition_hook.harness",
    "transition_hook.prompt",
)


def read_key(config: dict[str, Any], key: str) -> Any:
    top, _, sub = key.partition(".")
    value = config.get(top)
    if not sub:
        return value
    return value.get(sub, "") if isinstance(value, dict) else ""


def write_key(config: dict[str, Any], key: str, value: Any) -> None:
    top, _, sub = key.partition(".")
    if not sub:
        config[top] = value
        return
    hook = config.get(top)
    config[top] = {**(hook if isinstance(hook, dict) else {}), sub: value}


def flatten(values: dict[str, Any]) -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, value in values.items():
        if key == "transition_hook" and isinstance(value, dict):
            for sub in ("harness", "prompt"):
                if sub in value:
                    flat[f"transition_hook.{sub}"] = value[sub]
        elif key in CONFIG_KEYS:
            flat[key] = value
    return flat


def check_key(key: str) -> str:
    if key not in CONFIG_KEYS:
        raise ValueError(
            f"Unknown setting {key!r}. Known settings: {', '.join(CONFIG_KEYS)}"
        )
    return key


def coerce(key: str, value: Any) -> Any:
    check_key(key)
    if key == "max_parallel":
        if isinstance(value, bool) or (
            isinstance(value, float) and not value.is_integer()
        ):
            raise ValueError("max_parallel must be a whole number")
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise ValueError("max_parallel must be a whole number") from None
        if not 1 <= number <= MAX_PARALLEL_LIMIT:
            raise ValueError(f"max_parallel must be between 1 and {MAX_PARALLEL_LIMIT}")
        return number
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text")
    if key == "default_harness" and value not in HARNESSES:
        raise ValueError("default_harness must be one of " + ", ".join(HARNESSES))
    if key == "transition_hook.harness" and value not in ("", *HARNESSES):
        raise ValueError(
            "transition_hook.harness must be empty or one of " + ", ".join(HARNESSES)
        )
    limit = TEXT_LIMITS.get(key, TEXT_LIMIT)
    if len(value) > limit:
        raise ValueError(f"{key} is longer than {limit} characters")
    return value

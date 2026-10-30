import pytest

from kanbeat.native import command
from kanbeat.native_settings import (
    NativeSettings,
    SettingsError,
    cli_args,
    from_claude_statusline,
    from_codex_thread,
    from_opencode_session,
)


def test_empty_settings_keep_existing_commands():
    assert command("codex", "p", None)[0] == ["codex", "exec", "--json", "-"]
    assert command("codex", "p", "t1")[0] == [
        "codex",
        "exec",
        "resume",
        "t1",
        "-",
        "--json",
    ]
    assert command("claude", "p", "s")[0] == [
        "claude",
        "-p",
        "p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--resume",
        "s",
    ]
    assert command("opencode", "p", None)[0] == [
        "opencode",
        "run",
        "--format",
        "json",
        "p",
    ]
    assert command("pi", "p", "x")[0] == [
        "pi",
        "--mode",
        "json",
        "--session",
        "x",
        "p",
    ]


def test_commands_apply_validated_settings():
    codex = NativeSettings("codex", "gpt-x", "openai", "high")
    argv, stdin = command("codex", "p", None, codex)
    assert argv == [
        "codex",
        "exec",
        "-c",
        'model_provider="openai"',
        "-m",
        "gpt-x",
        "-c",
        'model_reasoning_effort="high"',
        "--json",
        "-",
    ]
    assert stdin == b"p"
    claude = NativeSettings("claude", "opus", effort="max")
    assert command("claude", "p", None, claude)[0][-4:] == [
        "--model",
        "opus",
        "--effort",
        "max",
    ]
    opencode = NativeSettings("opencode", "m", "p", "high")
    assert command("opencode", "p", None, opencode)[0] == [
        "opencode",
        "run",
        "--format",
        "json",
        "--model",
        "p/m",
        "--variant",
        "high",
        "p",
    ]
    assert cli_args("opencode", opencode, interactive=True) == ["--model", "p/m"]


@pytest.mark.parametrize(
    "harness,effort",
    [("claude", "none"), ("codex", "max"), ("pi", "max")],
)
def test_effort_is_strict_per_harness(harness, effort):
    provider = "p" if harness != "claude" else None
    with pytest.raises(SettingsError, match="not supported"):
        NativeSettings(harness, "m", provider, effort)


@pytest.mark.parametrize(
    "value",
    ["--evil", "-m", "a b", "a\nb", "a\x00", "x" * 201, 5, ["a"], {"a": 1}],
)
def test_rejects_injection_and_non_scalars(value):
    with pytest.raises(SettingsError):
        NativeSettings("claude", value)


def test_extracts_supported_fields_only():
    codex = from_codex_thread(
        {
            "model": "gpt-x",
            "modelProvider": "openai",
            "reasoningEffort": "high",
            "preview": "SECRET",
        }
    )
    assert codex.to_dict() == {
        "model": "gpt-x",
        "provider": "openai",
        "effort": "high",
    }
    claude = from_claude_statusline(
        {
            "model": {"id": "claude-x", "display_name": "Claude"},
            "effort": {"level": "high"},
            "cost": {"total_cost_usd": 1},
        }
    )
    assert claude.to_dict() == {"model": "claude-x", "effort": "high"}
    opencode = from_opencode_session(
        {"model": {"providerID": "p", "id": "m", "variant": "high"}}
    )
    assert opencode.to_dict() == {"model": "m", "provider": "p", "effort": "high"}


def test_settings_round_trip_rejects_unknown_keys():
    settings = NativeSettings("codex", "m", "p", "low")
    assert NativeSettings.from_dict("codex", settings.to_dict()) == settings
    with pytest.raises(SettingsError, match="Unknown"):
        NativeSettings.from_dict("codex", {"sandbox": "danger-full-access"})
    with pytest.raises(SettingsError, match="provider and model"):
        NativeSettings("opencode", "m")

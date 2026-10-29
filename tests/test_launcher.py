"""Source versus frozen launch selection and external environment handling."""

import sys

import pytest

from kanbeat import launcher


@pytest.fixture
def frozen(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/opt/kanbeat/kanbeat")
    monkeypatch.setattr(sys, "_MEIPASS", "/opt/kanbeat/_internal", raising=False)


def test_source_commands_use_python_modules():
    assert launcher.app_command("--project", "/p", "supervisor") == [
        sys.executable,
        "-m",
        "kanbeat",
        "--project",
        "/p",
        "supervisor",
    ]
    assert launcher.pty_child_command(["codex", "x"]) == [
        sys.executable,
        "-m",
        "kanbeat.pty_child",
        "codex",
        "x",
    ]


def test_frozen_commands_never_pass_module_flag(frozen):
    assert launcher.app_command("--project", "/p", "supervisor") == [
        "/opt/kanbeat/kanbeat",
        "--project",
        "/p",
        "supervisor",
    ]
    command = launcher.pty_child_command(["codex", "x"])
    assert command == [
        "/opt/kanbeat/kanbeat",
        launcher.PTY_CHILD_FLAG,
        "codex",
        "x",
    ]
    assert "-m" not in command


def test_frozen_external_environment_restores_loader_paths(frozen):
    env = launcher.external_environment(
        {
            "PATH": "/bin",
            "LD_LIBRARY_PATH": "/opt/kanbeat/_internal",
            "LD_LIBRARY_PATH_ORIG": "/usr/local/lib",
            "DYLD_LIBRARY_PATH": "/opt/kanbeat/_internal",
            "_PYI_APPLICATION_HOME_DIR": "/opt/kanbeat/_internal",
            "_PYI_ARCHIVE_FILE": "/opt/kanbeat/kanbeat",
            "PYINSTALLER_RESET_ENVIRONMENT": "1",
        }
    )
    assert env == {"PATH": "/bin", "LD_LIBRARY_PATH": "/usr/local/lib"}


def test_frozen_external_environment_keeps_user_settings(frozen):
    env = launcher.external_environment(
        {"HOME": "/h", "CODEX_HOME": "/c", "LD_LIBRARY_PATH": "/opt/kanbeat/_internal"}
    )
    assert env == {"HOME": "/h", "CODEX_HOME": "/c"}


def test_source_external_environment_is_untouched():
    source = {"LD_LIBRARY_PATH": "/user/lib", "PATH": "/bin"}
    assert launcher.external_environment(source) == source


def test_frozen_internal_environment_requests_fresh_bootloader(frozen):
    env = launcher.internal_environment(
        {"LD_LIBRARY_PATH": "/opt/kanbeat/_internal", "LD_LIBRARY_PATH_ORIG": "/u"}
    )
    assert env == {"LD_LIBRARY_PATH": "/u", launcher.RESET_ENVIRONMENT: "1"}


def test_source_internal_environment_has_no_reset_flag():
    assert launcher.RESET_ENVIRONMENT not in launcher.internal_environment({"A": "1"})


def test_run_internal_dispatches_pty_child(monkeypatch):
    calls = []
    monkeypatch.setattr("kanbeat.pty_child.main", calls.append)
    assert launcher.run_internal(["kanbeat", launcher.PTY_CHILD_FLAG, "tool", "arg"])
    assert calls == [["kanbeat", "tool", "arg"]]
    assert not launcher.run_internal(["kanbeat", "list"])
    assert not launcher.run_internal(["kanbeat"])


def test_spawned_native_cli_receives_external_environment(tmp_path, monkeypatch):
    import asyncio

    from kanbeat.native import NativeRun

    captured = {}

    async def fake_exec(*args, **kwargs):
        captured.update(kwargs)
        raise OSError("stop")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/bundle")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/user")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr("kanbeat.native.command", lambda *a: (["tool"], None))
    run = NativeRun("codex", "p", None, tmp_path, tmp_path / "log")
    asyncio.run(run.execute())
    assert captured["env"]["LD_LIBRARY_PATH"] == "/user"
    assert "LD_LIBRARY_PATH_ORIG" not in captured["env"]


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_unrelated_user_loader_paths_are_preserved(frozen, monkeypatch, platform):
    monkeypatch.setattr(sys, "platform", platform)
    env = launcher.external_environment(
        {
            "LD_LIBRARY_PATH": "/user/ld",
            "LIBPATH": "/user/aix",
            "DYLD_LIBRARY_PATH": "/user/mac:/opt/kanbeat/_internal",
        }
    )
    assert env["LIBPATH"] == "/user/aix"
    assert env["DYLD_LIBRARY_PATH"] == "/user/mac"
    if platform == "darwin":
        assert env["LD_LIBRARY_PATH"] == "/user/ld"

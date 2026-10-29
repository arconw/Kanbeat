"""Command line behaviour: version output and foreground shutdown."""

import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from kanbeat import __version__
from kanbeat.ipc import socket_path


def run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "kanbeat", *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_version_flag_prints_package_version():
    result = run_cli("--version")
    assert result.returncode == 0
    assert result.stdout.strip() == f"kanbeat {__version__}"


def test_foreground_interrupt_cleans_up_without_traceback(tmp_path: Path):
    process = subprocess.Popen(
        [sys.executable, "-m", "kanbeat", "--project", str(tmp_path), "supervisor"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    sock = socket_path(tmp_path)
    try:
        deadline = time.monotonic() + 20
        while not sock.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert sock.exists()
        process.send_signal(signal.SIGINT)
        _, stderr = process.communicate(timeout=20)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    assert process.returncode == 130
    assert "Traceback" not in stderr
    assert not sock.exists()


def test_shutdown_command_waits_until_supervisor_exits(tmp_path: Path):
    process = subprocess.Popen(
        [sys.executable, "-m", "kanbeat", "--project", str(tmp_path), "supervisor"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    sock = socket_path(tmp_path)
    try:
        deadline = time.monotonic() + 20
        while not sock.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert sock.exists()
        result = run_cli("--project", str(tmp_path), "shutdown")
        assert result.returncode == 0, result.stderr
        _, stderr = process.communicate(timeout=10)
        assert process.returncode == 0
        assert "Traceback" not in stderr
        assert not sock.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()

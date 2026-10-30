"""Exercise an extracted standalone archive outside the source tree."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pty
import re
import signal
import socket
import struct
import subprocess
import sys
import tarfile
import termios
import time

from kanbeat.ipc import socket_path

MAX_SOCKET_BYTES = 103
LOADER_SENTINEL = "/kanbeat-smoke-user-library-path"
FIXTURE = """#!/bin/sh
echo "FIXTURE-CODEX tty=$([ -t 0 ] && echo yes || echo no)"
echo "FIXTURE-LD ${LD_LIBRARY_PATH-unset}"
echo "FIXTURE-LEAK $(env | grep -c '^_PYI_\\|^PYINSTALLER_\\|^_MEIPASS')"
echo "FIXTURE-READY"
while read -r line; do
  case "$line" in
    size) echo "FIXTURE-SIZE $(stty size)" ;;
    quit) exit 0 ;;
    *) echo "FIXTURE-ECHO $line" ;;
  esac
done
"""


def fail(message: str) -> None:
    raise SystemExit("smoke: " + message)


def check(condition: bool, message: str) -> None:
    if not condition:
        fail(message)
    print("ok  " + message)


def wait_for(predicate, message: str, timeout: float = 30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    fail("timed out: " + message)


def verify_checksum(archive: Path) -> None:
    recorded = archive.with_name(archive.name + ".sha256").read_text().split()
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    check(recorded == [digest, archive.name], "checksum file matches archive")


def extract(archive: Path, destination: Path) -> Path:
    destination.mkdir(parents=True)
    with tarfile.open(archive) as bundle:
        bundle.extractall(destination, filter="data")
    roots = list(destination.iterdir())
    check(
        len(roots) == 1 and (roots[0] / "kanbeat").is_file(),
        "archive holds one directory with the kanbeat executable",
    )
    return roots[0]


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def parent_pid(pid: int) -> int:
    output = subprocess.check_output(["ps", "-o", "ppid=", "-p", str(pid)], text=True)
    return int(output.strip())


class Smoke:
    def __init__(self, root: Path, work: Path):
        self.root = root
        self.executable = root / "kanbeat"
        self.project = work / "project"
        self.project.mkdir()
        fixtures = work / "fixture-bin"
        fixtures.mkdir()
        codex = fixtures / "codex"
        codex.write_text(FIXTURE)
        codex.chmod(0o755)
        scratch = work / "tmp"
        scratch.mkdir()
        self.env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("_PYI_", "PYINSTALLER_"))
        }
        self.env.update(
            PATH=f"{fixtures}{os.pathsep}/usr/bin{os.pathsep}/bin",
            LD_LIBRARY_PATH=LOADER_SENTINEL,
            TMPDIR=str(scratch),
        )
        self.socket = socket_path(self.project)
        self.supervisor: int | None = None
        if len(os.fsencode(self.socket)) > MAX_SOCKET_BYTES:
            fail("resolved runtime socket path is too long")

    def cli(self, *args: str) -> str:
        result = subprocess.run(
            [str(self.executable), "--project", str(self.project), *args],
            cwd=self.project,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            fail(f"{' '.join(args)} exited {result.returncode}: {result.stderr}")
        return result.stdout

    def ipc(self, payload: dict) -> dict:
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(10)
            client.connect(str(self.socket))
            client.sendall(json.dumps(payload).encode() + b"\n")
            reply = json.loads(client.makefile("rb").readline())
        if not reply.get("ok"):
            fail(f"IPC {payload['op']} failed: {reply.get('error')}")
        return reply["result"]

    def supervisor_pid(self) -> int:
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(str(self.socket))
            if sys.platform == "darwin":
                return client.getsockopt(0, 2)
            credentials = client.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
            )
            return struct.unpack("3i", credentials)[0]

    def terminal_text(self, terminal: str) -> str:
        return self.ipc({"op": "terminal_snapshot", "terminal": terminal})["text"]

    def version_and_resources(self, version: str) -> None:
        output = self.cli("--version").strip()
        check(output == f"kanbeat {version}", f"--version prints {output!r}")
        check("usage: kanbeat" in self.cli("--help"), "--help runs")
        for resource in (
            "kanbeat/ui/styles.tcss",
            "textual/tree-sitter/highlights/css.scm",
        ):
            check(
                any(self.root.rglob(resource)) or any(self.root.rglob(resource + "c")),
                f"bundle contains {resource}",
            )

    def supervisor_and_tui(self) -> int:
        self.cli("new", "Smoke")
        check(self.socket.exists(), "self-spawned supervisor socket exists")
        self.supervisor = self.supervisor_pid()
        snapshot = self.ipc({"op": "snapshot"})
        check(
            [t["title"] for t in snapshot["tickets"]] == ["Smoke"],
            "supervisor IPC returns the created ticket",
        )
        self.tui()
        return snapshot["tickets"][0]["id"]

    def tui(self) -> None:
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 150, 0, 0))
        process = subprocess.Popen(
            [str(self.executable), "--project", str(self.project), "run"],
            cwd=self.project,
            env={**self.env, "TERM": "xterm-256color"},
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
        )
        os.close(slave)
        os.set_blocking(master, False)
        seen = bytearray()

        def drain() -> bytearray:
            try:
                seen.extend(os.read(master, 65536))
            except (BlockingIOError, OSError):
                pass
            return seen

        try:
            wait_for(lambda: b"KANBEAT" in drain(), "TUI starts on the Sessions page")
            os.write(master, b"2")
            wait_for(
                lambda: b"Smoke" in drain(),
                "TUI renders the board with its stylesheet",
            )
            check(True, "TUI rendered the board from bundled resources")
            os.write(master, b"\x11")
            wait_for(
                lambda: drain() is not None and process.poll() is not None, "TUI exit"
            )
            check(process.poll() == 0, f"TUI quits cleanly on ctrl+q: {process.poll()}")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)

    def terminal(self, ticket: str) -> None:
        terminal = self.ipc({"op": "terminal_open", "ticket": ticket})["terminal"]
        text = wait_for(
            lambda: (
                "FIXTURE-READY" in self.terminal_text(terminal)
                and self.terminal_text(terminal)
            ),
            "fixture codex started in the PTY",
        )
        check("tty=yes" in text, "child has a controlling terminal")
        check(
            f"FIXTURE-LD {LOADER_SENTINEL}" in text,
            "external CLI sees the user's loader path",
        )
        check("FIXTURE-LEAK 0" in text, "no bundle variables reach the external CLI")
        self.ipc(
            {
                "op": "terminal_input",
                "terminal": terminal,
                "kind": "paste",
                "text": "hello\n",
            }
        )
        wait_for(
            lambda: "FIXTURE-ECHO hello" in self.terminal_text(terminal),
            "terminal input echoed",
        )
        check(True, "terminal input reaches the child")
        self.ipc(
            {"op": "terminal_resize", "terminal": terminal, "rows": 31, "cols": 101}
        )
        self.ipc(
            {
                "op": "terminal_input",
                "terminal": terminal,
                "kind": "paste",
                "text": "size\n",
            }
        )
        wait_for(
            lambda: "FIXTURE-SIZE 31 101" in self.terminal_text(terminal),
            "resize visible to child",
        )
        check(True, "terminal resize reaches the child")
        pid = self.ipc({"op": "terminal_snapshot", "terminal": terminal})["pid"]
        check(
            parent_pid(pid) == self.supervisor,
            "terminal child belongs to the supervisor",
        )
        self.ipc({"op": "terminal_stop", "terminal": terminal, "confirmed": True})
        wait_for(lambda: not process_exists(pid), "terminal child reaped")
        check(True, "stopped terminal child is gone and reaped")

    def shutdown(self) -> None:
        if self.supervisor is None:
            return
        os.kill(self.supervisor, signal.SIGINT)
        wait_for(lambda: not process_exists(self.supervisor), "supervisor exit")
        check(not self.socket.exists(), "supervisor exited and removed its socket")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--work", type=Path, required=True)
    args = parser.parse_args()
    archive, work = args.archive.resolve(), args.work.resolve()
    match = re.fullmatch(
        r"kanbeat-(.+)-(linux|macos)-(x86_64|arm64)\.tar\.gz", archive.name
    )
    if match is None:
        fail("unexpected archive name " + archive.name)
    verify_checksum(archive)
    root = extract(archive, work / "extracted")
    smoke = Smoke(root, work)
    try:
        smoke.version_and_resources(match.group(1))
        ticket = smoke.supervisor_and_tui()
        smoke.terminal(ticket)
    finally:
        smoke.shutdown()
    print("smoke: all checks passed")


if __name__ == "__main__":
    main()

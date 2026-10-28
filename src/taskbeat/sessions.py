"""Native CLI processes inside a private project-scoped tmux server.

No permissions, sandbox or authentication flags are supplied by TaskBeat.
The experimental embedded terminal is purely a view on an existing tmux session.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
from typing import Any

from .storage import TicketStore, atomic_write


class SessionError(RuntimeError):
    pass


class TmuxSessions:
    def __init__(self, store: TicketStore):
        self.store = store
        self.socket = store.state_dir / "tmux.sock"
        self.registry_path = store.state_dir / "sessions.json"

    def _command(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        if shutil.which("tmux") is None:
            raise SessionError("tmux is required; install tmux to run native harnesses")
        result = subprocess.run(
            ["tmux", "-S", str(self.socket), *args], cwd=self.store.project,
            capture_output=True, text=True, check=False,
        )
        if check and result.returncode:
            raise SessionError(result.stderr.strip() or "tmux failed")
        return result

    def target(self, ticket_id: str, role_id: str, harness: str) -> str:
        digest = hashlib.sha256(
            f"{self.store.project}:{ticket_id}:{role_id}:{harness}".encode()
        ).hexdigest()[:20]
        return "tb-" + digest

    def is_alive(self, target: str | None) -> bool:
        if not target:
            return False
        return self._command("has-session", "-t", "=" + target, check=False).returncode == 0

    def load_registry(self) -> dict[str, dict[str, Any]]:
        if not self.registry_path.exists():
            return {}
        data = json.loads(self.registry_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}

    def register(self, target: str, **attributes: Any) -> None:
        with self.store.locked():
            registry = self.load_registry()
            registry[target] = {
                **registry.get(target, {}),
                "target": target,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                **attributes,
            }
            atomic_write(self.registry_path, json.dumps(registry, indent=2) + "\n")

    def start(
        self, target: str, harness: str, command: list[str], *,
        ticket_id: str | None = None, role_id: str | None = None,
        group: str = "Tasks",
    ) -> bool:
        """Return True if created; False if reusing a live session."""
        if not command or not all(isinstance(part, str) for part in command):
            raise SessionError(f"Invalid command for harness {harness}")
        if not shutil.which(command[0]):
            raise SessionError(f"Executable is unavailable: {command[0]}")
        alive = self.is_alive(target)
        if not alive:
            # A single configured command string is shell-parsed by tmux,
            # but task content is never interpolated into it.
            self._command(
                "new-session", "-d", "-s", target, "-c", str(self.store.project),
                shlex.join(command),
            )
        self.register(
            target, harness=harness, command=command, ticket_id=ticket_id,
            role_id=role_id, group=group, status="running",
        )
        return not alive

    def send(self, target: str, message: str) -> None:
        if not self.is_alive(target):
            raise SessionError("Session is not alive: " + target)
        # A private file and a named tmux buffer prevent prompts from being
        # interpreted as shell arguments or leaking via process argv.
        name = "taskbeat-" + hashlib.sha1(target.encode()).hexdigest()[:14]
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self.store.state_dir, delete=False
        ) as stream:
            temp = Path(stream.name)
            stream.write(message)
        try:
            temp.chmod(0o600)
            self._command("load-buffer", "-b", name, str(temp))
            self._command("paste-buffer", "-b", name, "-d", "-t", "=" + target)
            self._command("send-keys", "-t", "=" + target, "Enter")
        finally:
            temp.unlink(missing_ok=True)
            self._command("delete-buffer", "-b", name, check=False)

    def capture(self, target: str, lines: int = 180) -> str:
        if not self.is_alive(target):
            return "[session offline]"
        result = self._command(
            "capture-pane", "-p", "-S", f"-{min(max(lines, 1), 2000)}",
            "-t", "=" + target, check=False,
        )
        return result.stdout[-100_000:] if result.returncode == 0 else "[capture unavailable]"

    def stop(self, target: str, *, force: bool = False) -> None:
        entry = self.load_registry().get(target, {})
        if entry.get("ticket_id") and not force:
            raise SessionError("Move the ticket to Backlog/Archive before stopping its harness")
        self._command("kill-session", "-t", "=" + target, check=False)
        self.register(target, status="stopped")

    def create_custom(self, name: str, harness: str, group: str) -> str:
        commands = self.store.config()["harness_commands"]
        if harness not in commands:
            raise SessionError("Unknown harness: " + harness)
        target = self.target("custom-" + name, group, harness)
        self.start(target, harness, commands[harness], group=group)
        self.register(target, title=name)
        return target

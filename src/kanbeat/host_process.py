from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import signal
from typing import Any

from .native import process_identity, terminate_process

CHILD_UMASK = 0o077


class HostError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeIdentity:
    kind: str
    pid: int
    identity: str
    endpoint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "pid": self.pid,
            "identity": self.identity,
            "endpoint": self.endpoint,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "RuntimeIdentity":
        if not isinstance(data, dict):
            raise HostError("Runtime identity must be an object")
        kind, pid, identity, endpoint = (
            data.get("kind"),
            data.get("pid"),
            data.get("identity"),
            data.get("endpoint"),
        )
        if (
            not isinstance(kind, str)
            or not isinstance(pid, int)
            or isinstance(pid, bool)
            or pid <= 1
            or not isinstance(identity, str)
            or not identity
            or not isinstance(endpoint, str)
        ):
            raise HostError("Runtime identity is malformed")
        return cls(kind, pid, identity, endpoint)


async def spawn_owned_group(
    argv: list[str],
    *,
    env: dict[str, str],
    cwd: Path,
    capture_output: bool,
) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE
        if capture_output
        else asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.STDOUT
        if capture_output
        else asyncio.subprocess.DEVNULL,
        env=env,
        cwd=str(cwd),
        start_new_session=True,
        umask=CHILD_UMASK,
    )


def identity_of(
    kind: str, proc: asyncio.subprocess.Process, endpoint: str
) -> RuntimeIdentity:
    identity = process_identity(proc.pid)
    if identity is None:
        raise HostError(f"Cannot fingerprint the {kind} host process")
    return RuntimeIdentity(kind, proc.pid, identity, endpoint)


async def stop_owned(proc: asyncio.subprocess.Process | None) -> None:
    await terminate_process(proc)


async def stop_recorded(runtime: RuntimeIdentity, *, grace: float = 3.0) -> bool:
    def matches() -> bool:
        return process_identity(runtime.pid) == runtime.identity

    def own_group() -> bool:
        try:
            return os.getpgid(runtime.pid) == runtime.pid
        except ProcessLookupError:
            return False

    if not matches() or not own_group():
        return False
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(runtime.pid, sig)
        except ProcessLookupError:
            return True
        deadline = asyncio.get_running_loop().time() + grace
        while asyncio.get_running_loop().time() < deadline:
            if not matches():
                return True
            await asyncio.sleep(0.05)
    return not matches()

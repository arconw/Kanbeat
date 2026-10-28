from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import socket

MAX_SOCKET_BYTES = 103


def socket_root() -> Path:
    return Path.home() / ".tmp" / "kanbeat-sockets"


def socket_path(project: Path) -> Path:
    project = project.expanduser().resolve()
    local = project / ".kanbeat" / ".state" / "supervisor.sock"
    if len(os.fsencode(local)) <= MAX_SOCKET_BYTES:
        return local
    name = hashlib.sha256(os.fsencode(project)).hexdigest()[:32] + ".sock"
    fallback = socket_root() / name
    if len(os.fsencode(fallback)) > MAX_SOCKET_BYTES:
        raise RuntimeError("Home runtime socket path exceeds the Unix socket limit")
    return fallback


def prepare_socket_directory(path: Path) -> None:
    directory = path.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError(
            "Socket directory must be a real directory owned by this user"
        )
    os.chmod(directory, 0o700)
    parent = directory.parent.stat()
    if directory == socket_root() and (
        parent.st_uid != os.getuid() or parent.st_mode & 0o022
    ):
        raise RuntimeError(
            "Socket directory parent must be owned by this user and not writable by others"
        )


def remove_socket(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError("Refusing to remove a non-owned or non-socket runtime path")
    path.unlink()


def bind_socket(path: Path) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    previous = os.umask(0o177)
    try:
        sock.bind(str(path))
        sock.setblocking(False)
        return sock
    except BaseException:
        sock.close()
        raise
    finally:
        os.umask(previous)

"""Process launch helpers that work from source and from a frozen bundle."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence

PTY_CHILD_FLAG = "--kanbeat-pty-child"
RESET_ENVIRONMENT = "PYINSTALLER_RESET_ENVIRONMENT"
LOADER_VARIABLES = (
    "LD_LIBRARY_PATH",
    "LIBPATH",
    "DYLD_LIBRARY_PATH",
    "DYLD_FALLBACK_LIBRARY_PATH",
)
BUNDLE_PREFIX = "_PYI_"
LEGACY_BUNDLE_VARIABLES = ("_MEIPASS2",)


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_command(*args: str) -> list[str]:
    if is_frozen():
        return [sys.executable, *args]
    return [sys.executable, "-m", "kanbeat", *args]


def pty_child_command(argv: Sequence[str]) -> list[str]:
    if is_frozen():
        return [sys.executable, PTY_CHILD_FLAG, *argv]
    return [sys.executable, "-m", "kanbeat.pty_child", *argv]


def external_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Environment for third-party programs, without bundle loader overrides."""
    env = dict(os.environ if source is None else source)
    if not is_frozen() and not any(key.startswith(BUNDLE_PREFIX) for key in env):
        return env
    for name in LOADER_VARIABLES:
        if name + "_ORIG" in env:
            env[name] = env.pop(name + "_ORIG")
            continue
        native_loader = (
            sys.platform.startswith("linux") and name == "LD_LIBRARY_PATH"
        ) or (sys.platform == "aix" and name == "LIBPATH")
        if native_loader:
            env.pop(name, None)
            continue
        bundle = str(getattr(sys, "_MEIPASS", ""))
        if bundle and name in env:
            paths = [
                part
                for part in env[name].split(os.pathsep)
                if part != bundle and not part.startswith(bundle + os.sep)
            ]
            if paths:
                env[name] = os.pathsep.join(paths)
            else:
                env.pop(name, None)
    for key in [key for key in env if key.startswith(BUNDLE_PREFIX)]:
        del env[key]
    for key in LEGACY_BUNDLE_VARIABLES:
        env.pop(key, None)
    env.pop(RESET_ENVIRONMENT, None)
    return env


def internal_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Environment for a fresh instance of this application."""
    env = external_environment(source)
    if is_frozen():
        env[RESET_ENVIRONMENT] = "1"
    return env


def run_internal(argv: Sequence[str]) -> bool:
    """Handle hidden self-spawn entry points; return whether one ran."""
    if len(argv) > 1 and argv[1] == PTY_CHILD_FLAG:
        from .pty_child import main

        main([argv[0], *argv[2:]])
        return True
    return False

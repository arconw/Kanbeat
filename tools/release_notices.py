"""Collect upstream licence texts for the packages bundled in a release."""

from __future__ import annotations

from importlib import metadata
import re

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

NOTICE_FILE = re.compile(r"(^|/)(LICEN[CS]E|COPYING|NOTICE|AUTHORS)[^/]*$", re.I)

RUNTIME_NOTE = """\
Kanbeat itself currently has no licence file; the project owner has not
published licence terms. Nothing in this archive grants rights beyond those in
the upstream notices below.

This archive is a PyInstaller onedir bundle. It also contains the CPython
runtime (Python Software Foundation License, https://docs.python.org/3/license.html),
the PyInstaller bootloader (GPL-2.0-or-later with a bootloader exception,
https://pyinstaller.org/en/stable/license.html) and the compiled libraries of
the Python standard library, including OpenSSL, SQLite, libffi and zlib, under
their own licences.
"""


def dependency_closure(roots: list[str]) -> list[str]:
    seen: dict[str, str] = {}
    visited: set[tuple[str, frozenset[str]]] = set()
    pending = [Requirement(root) for root in roots]
    while pending:
        requirement = pending.pop()
        key = canonicalize_name(requirement.name)
        visit = (key, frozenset(requirement.extras))
        if visit in visited:
            continue
        visited.add(visit)
        try:
            dist = metadata.distribution(requirement.name)
        except metadata.PackageNotFoundError:
            continue
        seen[key] = dist.metadata["Name"]
        extras = {""} | set(requirement.extras)
        for line in dist.requires or []:
            child = Requirement(line)
            if child.marker is None or any(
                child.marker.evaluate({"extra": extra}) for extra in extras
            ):
                pending.append(child)
    return sorted(seen.values(), key=str.lower)


def distribution_notices(name: str) -> str:
    dist = metadata.distribution(name)
    sections = [f"== {dist.metadata['Name']} {dist.version} =="]
    license_name = dist.metadata.get("License-Expression") or dist.metadata.get(
        "License"
    )
    if license_name and len(license_name) < 200:
        sections.append(f"License: {license_name}")
    for file in dist.files or []:
        if NOTICE_FILE.search(file.as_posix()):
            sections.append(f"-- {file.name} --\n{file.read_text()}")
    return "\n\n".join(sections)


def collect(runtime_requirements: list[str], bundled_tools: tuple[str, ...]) -> str:
    names = dependency_closure([*runtime_requirements, *bundled_tools])
    body = "\n\n".join(
        distribution_notices(name) for name in sorted(names, key=str.lower)
    )
    return "THIRD-PARTY NOTICES\n\n" + RUNTIME_NOTE + "\n" + body + "\n"

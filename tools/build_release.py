"""Build a standalone onedir archive of Kanbeat with PyInstaller."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tomllib

from release_notices import collect

ROOT = Path(__file__).resolve().parent.parent

COLLECTED_PACKAGES = ("kanbeat", "textual", "textual_tty", "bittty", "rich")
BUNDLED_TOOLS = ("pyinstaller", "pyinstaller-hooks-contrib", "altgraph")
EXCLUDED_MODULES = ("PIL", "tkinter", "pytest")
DOCUMENTS = ("README.md", "docs")
ARCHITECTURES = {
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "arm64": "arm64",
    "aarch64": "arm64",
}
SYSTEMS = {"linux": "linux", "darwin": "macos"}


def platform_label() -> str:
    system = SYSTEMS.get(sys.platform.split("-")[0].lower())
    machine = ARCHITECTURES.get(platform.machine().lower())
    if system is None or machine is None:
        raise SystemExit(f"Unsupported build host: {sys.platform} {platform.machine()}")
    return f"{system}-{machine}"


def package_version() -> str:
    from kanbeat import __version__

    return __version__


def run_pyinstaller(work: Path) -> Path:
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--name",
        "kanbeat",
        "--distpath",
        str(work / "dist"),
        "--workpath",
        str(work / "build"),
        "--specpath",
        str(work / "spec"),
    ]
    for package in COLLECTED_PACKAGES:
        command += ["--collect-submodules", package, "--collect-data", package]
    for module in EXCLUDED_MODULES:
        command += ["--exclude-module", module]
    command.append(str(ROOT / "tools" / "frozen_entry.py"))
    env = {**os.environ, "PYINSTALLER_CONFIG_DIR": str(work / "pyinstaller-config")}
    subprocess.run(command, check=True, cwd=work, env=env)
    return work / "dist" / "kanbeat"


def stage(bundle: Path, work: Path, name: str) -> Path:
    staging = work / "stage" / name
    shutil.rmtree(staging.parent, ignore_errors=True)
    staging.parent.mkdir(parents=True)
    shutil.copytree(bundle, staging, symlinks=True)
    for document in DOCUMENTS:
        source = ROOT / document
        destination = staging / document
        if source.is_dir():
            shutil.copytree(source, destination)
        else:
            shutil.copy2(source, destination)
    if (ROOT / "LICENSE").exists():
        shutil.copy2(ROOT / "LICENSE", staging / "LICENSE")
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    notices = collect(pyproject["project"]["dependencies"], BUNDLED_TOOLS)
    (staging / "THIRD_PARTY_NOTICES.txt").write_text(notices, encoding="utf-8")
    return staging


def reset_ownership(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def archive(staging: Path, output: Path) -> Path:
    target = output / f"{staging.name}.tar.gz"
    target.unlink(missing_ok=True)
    with tarfile.open(target, "w:gz") as bundle:
        bundle.add(staging, arcname=staging.name, filter=reset_ownership)
    return target


def write_checksum(target: Path) -> Path:
    digest = hashlib.sha256()
    with target.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    checksum = target.with_name(target.name + ".sha256")
    checksum.write_text(f"{digest.hexdigest()}  {target.name}\n", encoding="utf-8")
    return checksum


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    args = parser.parse_args()
    output, work = args.output.resolve(), args.work.resolve()
    output.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    name = f"kanbeat-{package_version()}-{platform_label()}"
    bundle = run_pyinstaller(work)
    target = archive(stage(bundle, work, name), output)
    print(target)
    print(write_checksum(target))


if __name__ == "__main__":
    main()

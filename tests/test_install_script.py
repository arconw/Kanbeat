import hashlib
import io
import os
import stat
import subprocess
import tarfile
from pathlib import Path


SCRIPT = Path(__file__).resolve().parent.parent / "install.sh"
TARGET = "linux-x86_64"


def release(base: Path, version: str, banner: str | None = None) -> None:
    folder = base / f"v{version}"
    folder.mkdir(parents=True)
    name = f"kanbeat-{version}-{TARGET}"
    program = f"#!/bin/sh\necho '{banner or f'kanbeat {version}'}'\n".encode()
    archive = folder / f"{name}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo(f"{name}/kanbeat")
        info.size = len(program)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(program))
        extra = tarfile.TarInfo(f"{name}/_internal/data.txt")
        extra.size = 2
        tar.addfile(extra, io.BytesIO(b"ok"))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (folder / "SHA256SUMS").write_text(
        f"{digest}  {archive.name}\n{'0' * 64}  other-file.whl\n"
    )


def run(tmp_path: Path, *arguments: str, version: str | None = "0.9.9", **extra):
    base = tmp_path / "releases"
    environment = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path / "home"),
        "KANBEAT_BASE_URL": base.as_uri(),
        "KANBEAT_TARGET": TARGET,
        **extra,
    }
    if version is not None:
        environment["KANBEAT_VERSION"] = version
    return subprocess.run(
        ["sh", str(SCRIPT), *arguments],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_script_is_a_posix_shell_script_that_is_executable():
    assert SCRIPT.read_text().startswith("#!/bin/sh\n")
    assert SCRIPT.stat().st_mode & stat.S_IXUSR
    assert subprocess.run(["sh", "-n", str(SCRIPT)]).returncode == 0


def test_install_verifies_unpacks_links_and_runs_the_binary(tmp_path):
    release(tmp_path / "releases", "0.9.9")
    result = run(tmp_path)
    assert result.returncode == 0, result.stderr
    prefix = tmp_path / "home" / ".local"
    assert "Checksum verified" in result.stdout
    assert "kanbeat 0.9.9" in result.stdout
    assert (prefix / "opt" / "kanbeat-0.9.9" / "_internal" / "data.txt").exists()
    assert (prefix / "opt" / "kanbeat").resolve() == (
        prefix / "opt" / "kanbeat-0.9.9"
    ).resolve()
    assert (prefix / "bin" / "kanbeat").is_symlink()
    shown = subprocess.run(
        [str(prefix / "bin" / "kanbeat")], capture_output=True, text=True
    )
    assert shown.stdout.strip() == "kanbeat 0.9.9"
    assert "to your PATH" in result.stdout


def test_a_wrong_checksum_installs_nothing(tmp_path):
    release(tmp_path / "releases", "0.9.9")
    sums = tmp_path / "releases" / "v0.9.9" / "SHA256SUMS"
    sums.write_text(sums.read_text().replace(sums.read_text()[:8], "00000000", 1))
    result = run(tmp_path)
    assert result.returncode != 0
    assert "checksum mismatch" in result.stderr
    assert not (tmp_path / "home" / ".local" / "opt").exists()
    assert not (tmp_path / "home" / ".local" / "bin").exists()


def test_an_archive_missing_from_the_checksums_is_refused(tmp_path):
    release(tmp_path / "releases", "0.9.9")
    sums = tmp_path / "releases" / "v0.9.9" / "SHA256SUMS"
    sums.write_text(f"{'1' * 64}  other-file.whl\n")
    result = run(tmp_path)
    assert result.returncode != 0
    assert "not listed in SHA256SUMS" in result.stderr


def test_a_missing_release_fails_cleanly(tmp_path):
    result = run(tmp_path, version="1.2.3")
    assert result.returncode != 0
    assert "download failed" in result.stderr


def test_updating_switches_the_links_and_keeps_the_old_version(tmp_path):
    release(tmp_path / "releases", "0.9.9")
    release(tmp_path / "releases", "1.0.0", banner="kanbeat one")
    assert run(tmp_path).returncode == 0
    result = run(tmp_path, version="v1.0.0")
    assert result.returncode == 0, result.stderr
    prefix = tmp_path / "home" / ".local"
    assert (prefix / "opt" / "kanbeat").resolve().name == "kanbeat-1.0.0"
    assert (prefix / "opt" / "kanbeat-0.9.9").is_dir()
    shown = subprocess.run(
        [str(prefix / "bin" / "kanbeat")], capture_output=True, text=True
    )
    assert shown.stdout.strip() == "kanbeat one"


def test_the_prefix_can_be_chosen_and_path_hint_is_skipped_when_present(tmp_path):
    release(tmp_path / "releases", "0.9.9")
    prefix = tmp_path / "custom"
    result = run(
        tmp_path,
        KANBEAT_PREFIX=str(prefix),
        PATH=f"{prefix / 'bin'}{os.pathsep}{os.environ['PATH']}",
    )
    assert result.returncode == 0, result.stderr
    assert (prefix / "bin" / "kanbeat").is_symlink()
    assert "to your PATH" not in result.stdout


def test_uninstall_removes_only_what_it_installed(tmp_path):
    release(tmp_path / "releases", "0.9.9")
    assert run(tmp_path).returncode == 0
    prefix = tmp_path / "home" / ".local"
    other = prefix / "bin" / "other-tool"
    other.write_text("keep")
    project = tmp_path / "project" / ".kanbeat"
    project.mkdir(parents=True)
    result = run(tmp_path, "--uninstall")
    assert result.returncode == 0, result.stderr
    assert not (prefix / "bin" / "kanbeat").exists()
    assert not (prefix / "bin" / "kanbeat").is_symlink()
    assert not (prefix / "opt" / "kanbeat").exists()
    assert not list((prefix / "opt").glob("kanbeat-*"))
    assert other.read_text() == "keep"
    assert project.is_dir()


def test_uninstall_leaves_a_foreign_link_alone(tmp_path):
    prefix = tmp_path / "home" / ".local"
    (prefix / "bin").mkdir(parents=True)
    foreign = tmp_path / "somewhere-else"
    foreign.write_text("#!/bin/sh\n")
    (prefix / "bin" / "kanbeat").symlink_to(foreign)
    result = run(tmp_path, "--uninstall")
    assert result.returncode == 0
    assert (prefix / "bin" / "kanbeat").is_symlink()
    assert "left in place" in result.stdout


def test_unsupported_systems_and_bad_arguments_are_refused(tmp_path):
    shim = tmp_path / "shim"
    shim.mkdir()
    uname = shim / "uname"
    uname.write_text('#!/bin/sh\n[ "$1" = "-s" ] && echo Plan9 || echo mips\n')
    uname.chmod(0o755)
    environment = {
        "PATH": f"{shim}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path / "home"),
    }
    refused = subprocess.run(
        ["sh", str(SCRIPT)], env=environment, capture_output=True, text=True
    )
    assert refused.returncode != 0
    assert "unsupported system: Plan9 mips" in refused.stderr
    wrong = run(tmp_path, "--bogus")
    assert wrong.returncode == 2
    assert "Usage: install.sh" in wrong.stderr
    helped = run(tmp_path, "--help")
    assert helped.returncode == 0 and "KANBEAT_VERSION" in helped.stdout

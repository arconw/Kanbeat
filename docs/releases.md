# Releases and standalone archives

Kanbeat 0.6.0 adds a release path: a wheel and sdist for Python installs, and standalone PyInstaller **onedir** archives that bundle their own Python runtime. Onedir is used instead of onefile because the supervisor is a long-lived background process: a onefile build unpacks itself into a temporary directory that must outlive the first client and survive restarts.

## Current status

- No release containing standalone archives has been published yet. The Python quick-start pins the tested `v0.5.1` tag, which installs from Git as described in the [README](../README.md#installation).
- Archives are built by the `Release` workflow. A pull request builds and smoke-tests them without publishing. A reviewed `v*` tag builds them again and prepares a **draft** GitHub release for the owner to inspect and publish by hand.
- Kanbeat is not on PyPI or Homebrew, and the workflow never publishes there.
- There is no project licence yet. Archives ship `THIRD_PARTY_NOTICES.txt` with the upstream licence texts of the bundled packages and a note on the CPython runtime and PyInstaller bootloader. They do not grant rights to Kanbeat itself.

## Targets

These are the native build/test targets. An archive is uploaded only after its
smoke job passes; inspect the [Release workflow runs](https://github.com/arconw/Kanbeat/actions/workflows/release.yml)
for the exact version/commit. The matrix configuration alone is not a completed verification.

| Archive suffix | Build and smoke runner |
| --- | --- |
| `linux-x86_64` | `ubuntu-22.04` |
| `linux-arm64` | `ubuntu-22.04-arm` |
| `macos-arm64` | `macos-15` |
| `macos-x86_64` | `macos-15-intel` |

Linux archives are built on Ubuntu 22.04 (glibc 2.35) and need a distribution with an equal or newer glibc. They are not musl/Alpine builds. Windows, native or under WSL from a Windows host, is not covered: see [#11](https://github.com/arconw/Kanbeat/issues/11) and [#17](https://github.com/arconw/Kanbeat/issues/17).

macOS archives are **unsigned and not notarized**. Gatekeeper may block browser-downloaded copies; the manual trust step below is available after you verify the archive. Signing/notarization credentials are not configured by this workflow.

## Install from an archive

Download the archive for your platform and `SHA256SUMS` from the release page, then verify and unpack:

```sh
sha256sum --check --ignore-missing SHA256SUMS      # Linux
shasum -a 256 --check --ignore-missing SHA256SUMS  # macOS
tar -xzf kanbeat-0.6.0-linux-x86_64.tar.gz
mkdir -p ~/.local/opt ~/.local/bin
mv kanbeat-0.6.0-linux-x86_64 ~/.local/opt/kanbeat-0.6.0
ln -sfn ~/.local/opt/kanbeat-0.6.0 ~/.local/opt/kanbeat
ln -sf ~/.local/opt/kanbeat/kanbeat ~/.local/bin/kanbeat
kanbeat --version
```

Keep the `_internal` directory next to the `kanbeat` executable. Make sure `~/.local/bin` is on your `PATH`.

On macOS, a browser download carries a quarantine attribute. After verifying the checksum, remove it from the unpacked directory only if you trust the source:

```sh
xattr -dr com.apple.quarantine ~/.local/opt/kanbeat-0.6.0
```

Download and trust behavior depends on the download tool and local macOS policy.

## Update

For each active project, run `kanbeat --project /path/to/project shutdown` and close its clients. If independent sessions are running, the command requires `shutdown --yes` to confirm stopping them. Board `stop` alone keeps the supervisor alive and is not sufficient for switching binaries. Unpack the new version next to the old one and repoint the symlink:

```sh
ln -sfn ~/.local/opt/kanbeat-NEW ~/.local/opt/kanbeat
```

The `shutdown` command is available from 0.6.0. For older development versions, stop workers first and interrupt the project supervisor manually (Ctrl+C if it runs in the foreground).

Project data lives in each project's `.kanbeat/` directory and is unaffected.

## Remove

```sh
rm ~/.local/bin/kanbeat
rm ~/.local/opt/kanbeat
rm -rf ~/.local/opt/kanbeat-0.6.0
```

Your projects' `.kanbeat/` directories are not touched; delete them yourself if you no longer want the tickets.

## Python installs

Wheel and sdist installs are unchanged: `uv tool install`, `pipx install` or `pip` in a virtual environment, from the Git tag (see the README). The `Release` workflow also uploads the wheel and sdist as draft release assets.

## How a bundle behaves

- The frozen executable *is* Kanbeat, not a Python interpreter. The supervisor self-spawn and the terminal child use `kanbeat/launcher.py`: `kanbeat --project <dir> supervisor`, and an internal `--kanbeat-pty-child` flag handled before argument parsing. Source installs keep using `python -m kanbeat` and `python -m kanbeat.pty_child`.
- Native CLIs (`codex`, `claude`, `opencode`, `pi`) are started with the environment the user had, minus anything the bundle added: `LD_LIBRARY_PATH`/`DYLD_LIBRARY_PATH` are restored from PyInstaller's `*_ORIG` copies and `_PYI_*`/`PYINSTALLER_*` variables are removed. Their own login, permissions and sandbox settings are never touched.
- Internal children (supervisor, terminal child) are started with `PYINSTALLER_RESET_ENVIRONMENT=1` so each is a fresh bootloader run.
- `Ctrl+C` in a foreground `kanbeat supervisor` runs the normal shutdown (stops workers, closes terminals, removes the socket) and exits with status 130 without a traceback.

## Building locally

```sh
python -m venv .tmp/venv
.tmp/venv/bin/python -m pip install '.[release]'
.tmp/venv/bin/python tools/build_release.py --output .tmp/release/out --work .tmp/release/work
.tmp/venv/bin/python tools/smoke_release.py .tmp/release/out/kanbeat-0.6.0-linux-x86_64.tar.gz --work .tmp/release/smoke
```

`build_release.py` writes `kanbeat-<version>-<os>-<arch>.tar.gz` and a matching `.sha256`. `smoke_release.py` verifies the checksum, extracts into a fresh directory, and runs the extracted program outside the source tree against a fixture executable named `codex` on an isolated `PATH`. No vendor CLI or model is called. It checks:

- `--version` and `--help`, and the packaged stylesheet and Textual resources;
- a self-spawned supervisor reached over its real IPC socket;
- the TUI rendering the board in a pseudo-terminal and quitting;
- a terminal child with a controlling TTY that receives input and a resize;
- the external-CLI environment (a sentinel `LD_LIBRARY_PATH` is preserved and no bundle variable leaks);
- stopping the terminal reaps the child, and `SIGINT` shuts the supervisor down and removes its socket.

## Release workflow

`.github/workflows/release.yml` runs on pull requests that touch packaging, on `v*` tags and on manual dispatch. Every job has `contents: read` only, except the final `draft-release` job, which gets `contents: write`, runs only for `v*` tags and never for pull requests or forks. Actions are pinned by commit SHA.

For a tag the draft job downloads the verified artifacts, merges the per-archive checksums into `SHA256SUMS`, and creates a draft release with generated notes. If a release for the tag already exists, the job leaves published releases alone and only re-uploads assets to a draft. It never deletes a release.

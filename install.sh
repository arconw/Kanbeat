#!/bin/sh
set -eu

REPOSITORY="${KANBEAT_REPOSITORY:-arconw/Kanbeat}"
BASE_URL="${KANBEAT_BASE_URL:-https://github.com/$REPOSITORY/releases/download}"
PREFIX="${KANBEAT_PREFIX:-$HOME/.local}"
VERSION="${KANBEAT_VERSION:-}"
TARGET="${KANBEAT_TARGET:-}"

usage() {
    cat <<'TEXT'
Install Kanbeat from a release archive. No Python or Git needed.

Usage: install.sh [--uninstall] [--help]

Environment:
  KANBEAT_VERSION   release to install, for example 1.0.0 (default: the latest published release)
  KANBEAT_PREFIX    installation prefix (default: ~/.local)

The archive is unpacked to <prefix>/opt/kanbeat-<version>, and <prefix>/bin/kanbeat
points at it. Shell profiles and project data are never touched.
TEXT
}

fail() {
    printf 'install.sh: %s\n' "$1" >&2
    exit 1
}

say() {
    printf '%s\n' "$1"
}

need() {
    command -v "$1" >/dev/null 2>&1 || fail "$1 is required"
}

detect_target() {
    [ -n "$TARGET" ] && return
    system="$(uname -s)"
    machine="$(uname -m)"
    case "$system-$machine" in
        Linux-x86_64 | Linux-amd64) TARGET=linux-x86_64 ;;
        Linux-aarch64 | Linux-arm64) TARGET=linux-arm64 ;;
        Darwin-arm64) TARGET=macos-arm64 ;;
        Darwin-x86_64) TARGET=macos-x86_64 ;;
        *) fail "unsupported system: $system $machine (Linux and macOS on x86_64 or arm64 only)" ;;
    esac
}

latest_version() {
    final="$(curl -fsSL -o /dev/null -w '%{url_effective}' "https://github.com/$REPOSITORY/releases/latest")" ||
        fail "could not reach GitHub to find the latest release"
    tag="${final##*/}"
    case "$tag" in
        v[0-9]*) printf '%s\n' "${tag#v}" ;;
        *) fail "no published release found; set KANBEAT_VERSION to install a specific one" ;;
    esac
}

checksum_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d ' ' -f 1
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | cut -d ' ' -f 1
    else
        fail "sha256sum or shasum is required to verify the download"
    fi
}

download() {
    curl -fsSL -o "$2" "$1" || fail "download failed: $1"
}

install_release() {
    need curl
    need tar
    detect_target
    [ -n "$VERSION" ] || VERSION="$(latest_version)"
    VERSION="${VERSION#v}"
    archive="kanbeat-$VERSION-$TARGET.tar.gz"
    work="$(mktemp -d)"
    staging="$PREFIX/opt/.kanbeat-new-$$"
    trap 'rm -rf "$work" "$staging"' EXIT INT TERM

    say "Installing Kanbeat $VERSION for $TARGET"
    download "$BASE_URL/v$VERSION/$archive" "$work/$archive"
    download "$BASE_URL/v$VERSION/SHA256SUMS" "$work/SHA256SUMS"

    expected="$(awk -v name="$archive" '$2 == name { print $1 }' "$work/SHA256SUMS")"
    [ -n "$expected" ] || fail "$archive is not listed in SHA256SUMS"
    actual="$(checksum_of "$work/$archive")"
    [ "$expected" = "$actual" ] || fail "checksum mismatch for $archive; nothing was installed"
    say "Checksum verified"

    tar -xzf "$work/$archive" -C "$work"
    [ -x "$work/kanbeat-$VERSION-$TARGET/kanbeat" ] || fail "the archive does not contain the kanbeat executable"

    mkdir -p "$PREFIX/opt" "$PREFIX/bin"
    destination="$PREFIX/opt/kanbeat-$VERSION"
    rm -rf "$staging" "$staging-old"
    mv "$work/kanbeat-$VERSION-$TARGET" "$staging" || fail "could not stage the new files in $PREFIX/opt"
    if [ -e "$destination" ]; then
        mv "$destination" "$staging-old" || fail "could not move the existing $destination aside"
    fi
    if ! mv "$staging" "$destination"; then
        if [ -e "$staging-old" ]; then
            mv "$staging-old" "$destination" || say "could not restore the previous version; it remains in $staging-old"
        fi
        fail "could not install into $destination; the previous version was kept"
    fi
    rm -rf "$staging-old"
    ln -sfn "$destination" "$PREFIX/opt/kanbeat"
    ln -sf "$PREFIX/opt/kanbeat/kanbeat" "$PREFIX/bin/kanbeat"

    "$PREFIX/bin/kanbeat" --version || fail "the installed kanbeat does not run on this system"
    say "Installed to $destination"

    case ":$PATH:" in
        *":$PREFIX/bin:"*) ;;
        *) say "Add $PREFIX/bin to your PATH, for example: export PATH=\"$PREFIX/bin:\$PATH\"" ;;
    esac
    say "Updating: stop each project's supervisor first with 'kanbeat shutdown', then run this script again."
}

uninstall_release() {
    link="$PREFIX/bin/kanbeat"
    if [ -L "$link" ]; then
        case "$(readlink "$link")" in
            "$PREFIX"/opt/kanbeat/*) rm -f "$link" ;;
            *) say "$link does not point at an installation made by this script; left in place" ;;
        esac
    fi
    rm -f "$PREFIX/opt/kanbeat"
    for directory in "$PREFIX"/opt/kanbeat-*; do
        if [ -d "$directory" ]; then
            rm -rf "$directory"
        fi
    done
    say "Removed Kanbeat from $PREFIX. Project .kanbeat directories were not touched."
}

case "${1:-}" in
    "") install_release ;;
    --uninstall) uninstall_release ;;
    -h | --help) usage ;;
    *) usage >&2; exit 2 ;;
esac

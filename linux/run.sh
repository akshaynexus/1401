#!/bin/sh
# Runs 1401 straight from this checkout, without installing it. Sets up the packages first if any are missing.
#   linux/run.sh [--dry-run | --self-check]
set -eu
here=$(dirname "$(readlink -f "$0")")

if [ "$(id -u)" = 0 ]; then
    echo "1401: start it as yourself, not with sudo; it asks for the password only for the scan and the stick." >&2
    exit 1
fi
if [ -z "${WAYLAND_DISPLAY:-}" ] && [ -z "${DISPLAY:-}" ]; then
    echo "1401: no desktop session found (WAYLAND_DISPLAY and DISPLAY are empty). Run it from your desktop." >&2
    exit 1
fi

if command -v pacman >/dev/null 2>&1 && ! "$here/setup-arch.sh" --check >/dev/null 2>&1; then
    echo "1401: some packages are missing; installing them first."
    "$here/setup-arch.sh"
fi

exec "$here/1401" "$@"

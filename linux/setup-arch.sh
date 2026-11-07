#!/bin/sh
# Installs what 1401 needs on Arch Linux and CachyOS (and other pacman-based systems), then checks it.
#   linux/setup-arch.sh            install the packages (asks for your password once)
#   linux/setup-arch.sh --check    only report what is missing
set -eu

PACKAGES="python python-gobject gtk4 libadwaita webkitgtk-6.0 polkit util-linux dosfstools acpica"
here=$(dirname "$(readlink -f "$0")")

if [ "$(id -u)" = 0 ]; then
    echo "1401 setup: run this as yourself, not as root; it asks for the password when it installs." >&2
    exit 1
fi
if ! command -v pacman >/dev/null 2>&1; then
    echo "1401 setup: pacman was not found. This script is for Arch and CachyOS; docs/LINUX.md lists the packages for other systems." >&2
    exit 1
fi

missing=""
for p in $PACKAGES; do
    pacman -Qq "$p" >/dev/null 2>&1 || missing="$missing $p"
done

if [ "${1:-}" = "--check" ]; then
    if [ -n "$missing" ]; then echo "missing:$missing"; exit 1; fi
    echo "all packages are installed"; exit 0
fi

if [ -n "$missing" ]; then
    echo "1401 setup: installing$missing"
    if command -v sudo >/dev/null 2>&1; then
        # shellcheck disable=SC2086
        sudo pacman -S --needed --noconfirm $missing
    elif command -v pkexec >/dev/null 2>&1; then
        # shellcheck disable=SC2086
        pkexec pacman -S --needed --noconfirm $missing
    else
        echo "1401 setup: neither sudo nor pkexec is available; install as root: pacman -S --needed$missing" >&2
        exit 1
    fi
else
    echo "1401 setup: all packages are already installed"
fi

# The window and engine imports, and the tools the steps run.
fail=0
/usr/bin/python3 - <<'EOF' || fail=1
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw  # noqa: F401
print("ok   GTK", Gtk.get_major_version(), "/ libadwaita", Adw.get_major_version(), Adw.get_minor_version())
EOF
for t in iasl mkfs.fat sfdisk wipefs lsblk pkexec; do
    if command -v "$t" >/dev/null 2>&1 || [ -x "/usr/bin/$t" ] || [ -x "/usr/sbin/$t" ]; then
        echo "ok   $t"
    else
        echo "MISSING $t"; fail=1
    fi
done
if ! pgrep -f 'polkit-.*agent|polkit-gnome|polkit-kde|gnome-shell|xfce-polkit|lxpolkit|hyprpolkitagent|mate-polkit' >/dev/null 2>&1; then
    echo "note no polkit agent seems to be running: GNOME and KDE have one built in; on a bare window manager start one"
    echo "     (e.g. polkit-gnome or hyprpolkitagent), or the password prompt for the scan and the stick cannot appear."
fi
chmod +x "$here/1401" "$here/1401-helper" "$here/run.sh" 2>/dev/null || true
[ "$fail" = 0 ] && echo "1401 setup: done. Start it with linux/run.sh" || { echo "1401 setup: something is still missing (above)." >&2; exit 1; }

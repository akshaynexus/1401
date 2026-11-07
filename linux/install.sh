#!/bin/sh
# Installs 1401 for Linux from this folder.
#
#   linux/install.sh              into ~/.local (no root, except the polkit policy, which asks for sudo)
#   linux/install.sh --system     into /usr/local (everything root-owned; asks for sudo)
#   linux/install.sh --prefix DIR into DIR
#   linux/install.sh --no-polkit  skip the polkit policy (1401 then shows pkexec's generic password prompt)
#   linux/install.sh --uninstall  remove what an earlier run installed with the same options
#
# Layout: <prefix>/lib/1401 holds the engine (p1401, upstream) and linux/; <prefix>/bin/1401 starts the window;
# the .desktop file and icon go to <prefix>/share; the policy goes to /usr/share/polkit-1/actions (polkit reads only
# there and /etc/polkit-1/actions) with its exec.path set to <prefix>/lib/1401/linux/1401-helper.
set -eu

src=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
prefix="$HOME/.local"
system=0
polkit=1
uninstall=0
while [ $# -gt 0 ]; do
    case "$1" in
        --system) prefix=/usr/local; system=1 ;;
        --prefix) shift; prefix=$1 ;;
        --prefix=*) prefix=${1#--prefix=} ;;
        --no-polkit) polkit=0 ;;
        --uninstall) uninstall=1 ;;
        -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
        *) echo "install.sh: unknown option $1" >&2; exit 2 ;;
    esac
    shift
done

if [ "$(id -u)" = 0 ] && [ "$system" = 0 ] && [ "$prefix" = "$HOME/.local" ]; then
    echo "install.sh: run it as yourself (it asks for sudo where it needs it), or use --system." >&2
    exit 2
fi

as_root=""
if [ "$system" = 1 ] && [ "$(id -u)" != 0 ]; then as_root="sudo"; fi
sudo_cmd="sudo"
if [ "$(id -u)" = 0 ]; then sudo_cmd=""; fi

app="$prefix/lib/1401"
bin="$prefix/bin"
apps="$prefix/share/applications"
icons="$prefix/share/icons/hicolor/128x128/apps"
policy_dir=/usr/share/polkit-1/actions
policy="$policy_dir/com.nullmoth.A1401.policy"

if [ "$uninstall" = 1 ]; then
    $as_root rm -rf "$app"
    $as_root rm -f "$bin/1401" "$apps/com.nullmoth.A1401.desktop" "$icons/com.nullmoth.A1401.png"
    if [ "$polkit" = 1 ] && [ -f "$policy" ]; then $sudo_cmd rm -f "$policy"; fi
    echo "1401 removed from $prefix. Your working files stay in ${XDG_DATA_HOME:-$HOME/.local/share}/NullMoth/1401."
    exit 0
fi

for f in p1401/__init__.py upstream/PINNED.json linux/1401 linux/1401-helper linux/one401_gtk/app.py; do
    if [ ! -e "$src/$f" ]; then echo "install.sh: $src/$f is missing; run it from a complete 1401 folder." >&2; exit 1; fi
done

version=$(sed -n 's:.*<Version>\(.*\)</Version>.*:\1:p' "$src/windows/App/App.csproj" 2>/dev/null | head -n 1)
[ -n "$version" ] || version=$(cat "$src/linux/VERSION" 2>/dev/null || echo 0.0.0)

echo "Installing 1401 $version into $app"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
# the engine's own downloads (OCK_Files, Results, a fetched iasl) and Python caches are not copied
(cd "$src" && tar --exclude=__pycache__ --exclude='*.pyc' --exclude='upstream/OpCore-Simplify/OCK_Files' \
    --exclude='upstream/OpCore-Simplify/Results' --exclude='linux/VERSION' \
    -cf "$tmp/app.tar" p1401 upstream linux NOTICE.md LICENSE README.md docs)
$as_root rm -rf "$app"
$as_root mkdir -p "$app" "$bin" "$apps" "$icons"
$as_root tar -xf "$tmp/app.tar" -C "$app"
printf '%s\n' "$version" > "$tmp/VERSION"
$as_root cp "$tmp/VERSION" "$app/linux/VERSION"
$as_root chmod 755 "$app/linux/1401" "$app/linux/1401-helper"
if [ "$system" = 1 ]; then $as_root chown -R root:root "$app"; fi
$as_root ln -sf "$app/linux/1401" "$bin/1401"

sed "s|^Exec=.*|Exec=$bin/1401|" "$src/linux/com.nullmoth.A1401.desktop" > "$tmp/com.nullmoth.A1401.desktop"
$as_root install -m 644 "$tmp/com.nullmoth.A1401.desktop" "$apps/com.nullmoth.A1401.desktop"
$as_root install -m 644 "$src/linux/com.nullmoth.A1401.png" "$icons/com.nullmoth.A1401.png"
command -v update-desktop-database >/dev/null 2>&1 && $as_root update-desktop-database -q "$apps" 2>/dev/null || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && $as_root gtk-update-icon-cache -q -t "$prefix/share/icons/hicolor" 2>/dev/null || true

if [ "$polkit" = 1 ]; then
    sed "s|<annotate key=\"org.freedesktop.policykit.exec.path\">[^<]*</annotate>|<annotate key=\"org.freedesktop.policykit.exec.path\">$app/linux/1401-helper</annotate>|" \
        "$src/linux/com.nullmoth.A1401.policy" > "$tmp/com.nullmoth.A1401.policy"
    echo "Installing the polkit policy into $policy_dir (needs administrator rights)"
    if $sudo_cmd install -D -m 644 "$tmp/com.nullmoth.A1401.policy" "$policy"; then
        :
    else
        echo "The polkit policy was not installed. 1401 still works; the password prompt shows a generic message." >&2
        echo "To install it later:  sudo install -m 644 $tmp/com.nullmoth.A1401.policy $policy" >&2
        trap - EXIT
    fi
fi

if [ "$system" = 0 ]; then
    echo
    echo "Note: in this install the helper that pkexec runs as root ($app/linux/1401-helper) is owned by you."
    echo "pkexec asks for the administrator password every time, but any program running as you could change that"
    echo "file first. Use --system for a root-owned install if that matters on this PC."
fi
case ":$PATH:" in *":$bin:"*) ;; *) echo "Add $bin to PATH to start 1401 from a terminal; the menu entry works without it." ;; esac
echo "Done. Start 1401 from the applications menu or with $bin/1401."

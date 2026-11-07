# 1401 on Linux

The Linux version of 1401 is a GNOME window (GTK 4 and libadwaita) over the same engine the Windows app runs
(`p1401/`). It has the same steps in the same order: Check this PC, Build the Mac setup, Your BIOS steps, Create the
macOS stick. It also has the same links: scan for logs and send them, update the NVIDIA driver, update 1401, and
check your own EFI.

> Run 1401 on the PC you want macOS on. It reads that PC's hardware and ACPI tables, so the Linux system must be
> running on that PC itself, not in a virtual machine.

## Arch / CachyOS quick start

```bash
git clone -b linux-port https://github.com/akshaynexus/1401
cd 1401
linux/setup-arch.sh   # installs the packages below with pacman (asks for your password once) and checks them
linux/run.sh          # starts 1401 from the checkout; runs the setup first if anything is missing
```

Nothing is installed besides the packages; `linux/install.sh` is only for adding 1401 to the app menu.

## What you need

- Python 3.10 or newer with PyGObject.
- GTK 4.10 or newer, and libadwaita 1.5 or newer.
- WebKitGTK 6.0 (optional): it shows the BIOS steps inside the window. Without it, 1401 opens them in your browser.
- polkit and `pkexec`, plus a polkit agent. GNOME, KDE and most desktops start an agent for you.
- util-linux (`lsblk`, `findmnt`, `sfdisk`, `wipefs`) and dosfstools (`mkfs.fat`), which the stick writer uses.
- An internet connection, and a USB stick of 4 GB or more that can be erased.

| | Arch / CachyOS / Manjaro | Fedora | Debian 13 / Ubuntu 24.04 and newer |
|---|---|---|---|
| Python + PyGObject | `python python-gobject` | `python3 python3-gobject` | `python3 python3-gi` |
| GTK 4 + libadwaita | `gtk4 libadwaita` | `gtk4 libadwaita` | `gir1.2-gtk-4.0 gir1.2-adw-1` |
| WebKitGTK (optional) | `webkitgtk-6.0` | `webkitgtk6.0` | `gir1.2-webkit-6.0` |
| polkit | `polkit` | `polkit` | `pkexec` (older releases: `policykit-1`) |
| disk tools | `util-linux dosfstools` | `util-linux dosfstools` | `util-linux fdisk dosfstools` |
| iasl (ACPI compiler) | `acpica` | `acpica-tools` | `acpica-tools` |

```bash
sudo pacman -S --needed python python-gobject gtk4 libadwaita webkitgtk-6.0 polkit util-linux dosfstools acpica
sudo dnf install python3 python3-gobject gtk4 libadwaita webkitgtk6.0 polkit util-linux dosfstools acpica-tools
sudo apt install python3 python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-webkit-6.0 pkexec util-linux fdisk dosfstools acpica-tools
```

Debian 12 and Ubuntu 22.04 ship a libadwaita that is too old for this window.

## Install

From a copy of this repository:

```bash
linux/install.sh              # into ~/.local; asks for sudo only to install the polkit policy
linux/install.sh --system     # into /usr/local, everything owned by root (asks for sudo)
linux/install.sh --uninstall  # removes it again (add --system if you installed with it)
```

The script copies the engine and `linux/` to `<prefix>/lib/1401`. It links `<prefix>/bin/1401`, and installs the menu
entry (`com.nullmoth.A1401.desktop`) and its icon. It also installs the polkit policy
`/usr/share/polkit-1/actions/com.nullmoth.A1401.policy`, with its `exec.path` set to the installed
`linux/1401-helper`. `--no-polkit` skips the policy.

You can also run 1401 without installing it: `linux/1401` from the repository.

## Using it

Open **1401** from the applications menu, or run `1401` (or `linux/1401`). Do not start it with `sudo`. The window runs
as you, and only two steps ask for the administrator password:

- **Check this PC** reads the ACPI tables, which only root can read.
- **Create the macOS stick** erases and writes the stick.

Those two steps run `linux/1401-helper` through `pkexec`. The helper accepts only those two engine commands and
checks every argument. It writes only into folders that belong to you, and gives every file it creates back to you. You
are asked for the password every time; it is never kept.

The steps work as on Windows:

1. **Check this PC** (`p1401.scan`) writes the hardware report, the ACPI tables, a scan receipt and
   `hardware-map.txt` into a new `scan-<id>` folder.
2. **Build the Mac setup** (`python -m p1401 build`): choose macOS 15 Sequoia or macOS 26 Tahoe. 1401 builds the
   OpenCore EFI and checks it with OpenCore's own validator. The window shows the result when the build finishes.
3. **Your BIOS steps** (`p1401.guide`) shows the BIOS settings this build needs. Take a photo before you restart.
4. **Create the macOS stick** (`p1401.usbwriter write`): only USB sticks are offered. Before anything is erased, 1401
   names the stick and its device (for example `/dev/sdb`), and you tick that you understand everything on it will be
   erased. The writer checks the stick again by its id, size and model before erasing. A stick that holds a Mac or
   Windows system or an old installer is named, and 1401 asks you again.

If a step fails, 1401 saves a local report. It tells you it is sending that report to nullmothsystems.com, and sends it
only if you click OK. Your user name, host name and home folder are removed first.

### Where the files go

Working files go to `$XDG_DATA_HOME/NullMoth/1401`, which is usually `~/.local/share/NullMoth/1401`:

| File or folder | What it holds |
|---|---|
| `scan-<id>/` | the hardware report and ACPI tables |
| `efi/` | the build |
| `BIOS-steps.html` | your BIOS steps |
| `diagnostic-reports/` | the local reports |
| `OCK_Files/` | the engine's download cache |
| `NullMoth/` | the NVIDIA driver package that Update the NVIDIA driver downloads |

All of it belongs to you, including what the root steps wrote.

### Without pkexec

Without `pkexec`, or with no polkit agent running, 1401 says so and shows the exact command for that step. You can run
it in a terminal with `sudo`, for example:

```bash
sudo ~/.local/lib/1401/linux/1401-helper --lang en p1401.scan ~/.local/share/NullMoth/1401/scan-<id> --run-id <id>
```

When the polkit policy is not installed, `pkexec` still works, but the password prompt shows a generic message.

## Checking the window without a PC to scan

```bash
linux/1401 --dry-run            # the window, no engine step runs
linux/1401 --self-check         # checks the gating between pages through the real handlers, then exits
linux/1401 --screenshot DIR     # draws each page to DIR/1401-page<N>.png, then exits
python3 -m unittest tests.test_linux_gui
```

On a machine with no display, run these under `gtk4-broadwayd :5 &` with `GDK_BACKEND=broadway BROADWAY_DISPLAY=:5`.

## Acer Predator Helios 300 G3-571

Run on the Helios, 1401 recognises it from the scan (DMI product `Predator G3-571` and the GTX 1060 Mobile with
Acer's subsystem `1025:118b`). Its macOS 26 build then takes the GPU, Wi-Fi, Bluetooth and HDMI-audio drivers from
the pinned [macos_helios300](https://github.com/akshaynexus/macos_helios300) release (README, "Acer Predator Helios
300 G3-571"). Pick **macOS 26 Tahoe**: the release is built for it only. The stick is written the usual way; the
drivers macOS has to install itself (PG106Accel, the Metal plugin, NVK) are on it in `EFI/Helios300` with a README.

The profile was made from 1401's own Linux scan of the owner's laptop (`tests/fixtures/helios300/Report.json`, IDs
only; the laptop's ACPI tables are Acer firmware and stay out of git). With a copy of those tables,
`A1401_HELIOS_ACPI=<dir> python3 -m unittest tests.test_helios300` runs the full build and OpenCore's validator on it.

## How the Linux version differs from Windows

- **Root**: 1401 asks for the password through `pkexec` for the scan and the write. On Windows the whole app runs as
  administrator.
- **Update 1401**: a git checkout is updated with `git pull --ff-only`. An installed copy opens the releases page;
  run `linux/install.sh` from the new copy. If a newer version is out, 1401 asks you to update first. On Linux, *Not
  now* lets you carry on, because there is no Linux release package to swap in.
- **Translations**: the Chinese text is the Windows app's own (`windows/App/Loc.cs`, extracted to
  `linux/one401_gtk/loc_zh.json`). Lines that exist only on Linux are in English.

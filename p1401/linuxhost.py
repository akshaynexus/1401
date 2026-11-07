"""Linux host support: what the Windows build does through WMI, kernel32 and PowerShell, read from sysfs and lsblk.

Every function here is only called behind a Linux check, so the Windows path is unchanged. The pieces:

- work_dir(): $XDG_DATA_HOME/NullMoth/1401 (else ~/.local/share/NullMoth/1401), the Linux place for what the Windows
  app keeps in %LOCALAPPDATA%\\NullMoth\\1401.
- dump_acpi_tables(): the ACPI tables from /sys/firmware/acpi/tables, which only root can read. On Windows acpidump.exe
  does this; the Hardware Sniffer's own Linux dump shells out to an interactive `sudo cp` per table, which a GUI child
  process cannot answer, so 1401 copies them itself and says clearly when it is not root.
- storage_drive_rows(): the drive models (lsblk), in the same row shape as PowerShell Get-Disk on Windows.
- patch_sniffer(): three places the Sniffer's Linux collector reports differently from its Windows one, corrected so
  the build reads the same facts either way (physical cores, network device paths, RAID/VMD controllers).
- boot_epoch(): when the running kernel started, for the "report from before the last restart" check.
- give_back(): files the root scan wrote, handed back to the user who ran sudo/pkexec.

For a front end:
    python3 -m p1401.linuxhost workdir    prints the working-files folder
    python3 -m p1401.linuxhost acpi       exit 0 when the ACPI tables are readable (root), 1 when not, 2 when absent
"""
import json
import os
import re
import subprocess
import sys
import time

ACPI_TABLES = "/sys/firmware/acpi/tables"
PCI_DEVICES = "/sys/bus/pci/devices"
SYS_CPU = "/sys/devices/system/cpu"
APP = ("NullMoth", "1401")

ROOT_MESSAGE = ("Check this PC has to read the ACPI tables, and on Linux only root can read them "
                "(/sys/firmware/acpi/tables). Run the check as root (sudo or pkexec). It only reads; nothing on the PC "
                "is changed, and the files it writes are handed back to your user.")


class AcpiAccessError(RuntimeError):
    pass


def work_dir(env=None):
    """Where the working files go. XDG Base Directory: a relative XDG_DATA_HOME is invalid and ignored."""
    env = os.environ if env is None else env
    base = env.get("XDG_DATA_HOME") or ""
    if not os.path.isabs(base):
        home = env.get("HOME") or os.path.expanduser("~")
        base = os.path.join(home, ".local", "share")
    return os.path.join(base, *APP)


# --- ACPI tables -------------------------------------------------------------------------------------------------

def acpi_readable(src=ACPI_TABLES):
    """True when the DSDT can be read (as root), False when the folder is there but not readable, None when absent."""
    if not os.path.isdir(src):
        return None
    dsdt = os.path.join(src, "DSDT")
    return os.path.isfile(dsdt) and os.access(dsdt, os.R_OK)


def table_files(src=ACPI_TABLES):
    """The firmware's static tables: the plain files at the top of the folder. dynamic/ holds the SSDTs the firmware
    loaded later at run time (CPU power states), and data/ holds non-table blobs; acpidump on Windows writes neither."""
    out = []
    for name in sorted(os.listdir(src)):
        path = os.path.join(src, name)
        if os.path.isfile(path) and not os.path.islink(path) and not name.startswith("."):
            out.append(name)
    return out


def dump_acpi_tables(dest, src=ACPI_TABLES):
    """Copy the tables to dest/<name>.aml (lower case, as acpidump -b names them on Windows). Returns the names written.
    Raises AcpiAccessError with the reason when they cannot be read."""
    readable = acpi_readable(src)
    if readable is None:
        raise AcpiAccessError(f"{src} does not exist: this kernel exposes no ACPI tables (booted without ACPI?).")
    if not readable:
        raise AcpiAccessError(ROOT_MESSAGE)
    os.makedirs(dest, exist_ok=True)
    for name in os.listdir(dest):   # a fresh dump, as the Sniffer's own (remove_content=True)
        path = os.path.join(dest, name)
        if os.path.isfile(path) or os.path.islink(path):
            os.remove(path)
    written = []
    for name in table_files(src):
        try:
            with open(os.path.join(src, name), "rb") as fh:
                data = fh.read()
        except PermissionError:
            raise AcpiAccessError(ROOT_MESSAGE) from None
        except OSError:
            continue
        # every table starts with its 4-character signature; FACS is the one without the common 36-byte header
        if len(data) < 36 and not (data[:4] == b"FACS" and len(data) >= 32):
            continue
        target = os.path.join(dest, name.lower() + ".aml")
        with open(target + ".tmp", "wb") as fh:
            fh.write(data)
        os.replace(target + ".tmp", target)
        written.append(os.path.basename(target))
    return written


# --- storage drives (Get-Disk on Windows) ---------------------------------------------------------------------------

BUS_TYPES = {"nvme": "NVMe", "sata": "SATA", "ata": "ATA", "usb": "USB", "sas": "SAS", "scsi": "SCSI", "mmc": "SD",
             "virtio": "Virtual", "fc": "Fibre Channel", "iscsi": "iSCSI"}
SKIP_DISKS = ("zram", "loop", "ram", "nbd")
BOOT_MOUNTS = ("/",)
SYSTEM_MOUNTS = ("/boot", "/boot/efi", "/efi")


def _mounts(node):
    out = []
    for key in ("mountpoints", "mountpoint"):
        v = node.get(key)
        out += [m for m in (v if isinstance(v, list) else [v]) if isinstance(m, str) and m]
    for child in node.get("children") or []:
        if isinstance(child, dict):
            out += _mounts(child)
    return out


def _flag(v):
    return v is True or str(v).strip().lower() in ("1", "true")


def lsblk_rows(data):
    """lsblk -J output -> rows shaped like Get-Disk's (Number, Model, BusType, MediaType, Size, IsBoot, IsSystem). Pure."""
    rows = []
    for disk in (data or {}).get("blockdevices") or []:
        if not isinstance(disk, dict) or disk.get("type") != "disk":
            continue
        name = str(disk.get("name") or "")
        if not name or name.startswith(SKIP_DISKS):
            continue
        model = " ".join(str(disk.get("model") or "").split())
        if not model:
            continue
        tran = str(disk.get("tran") or "").strip().lower()
        mounts = _mounts(disk)
        try:
            size = int(disk.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        rows.append({"Number": name, "Model": model, "BusType": BUS_TYPES.get(tran, tran.upper()),
                     "MediaType": "HDD" if _flag(disk.get("rota")) else "SSD", "Size": size,
                     "IsBoot": any(m in BOOT_MOUNTS for m in mounts),
                     "IsSystem": any(m in SYSTEM_MOUNTS for m in mounts)})
    return rows


def storage_drive_rows(run=subprocess.run):
    """The installed drives via lsblk. Fail-safe like the Windows read: any error gives no rows."""
    for columns in ("NAME,MODEL,TRAN,ROTA,SIZE,TYPE,MOUNTPOINTS", "NAME,MODEL,TRAN,ROTA,SIZE,TYPE,MOUNTPOINT"):
        try:
            p = run(["lsblk", "-J", "-b", "-o", columns], capture_output=True, text=True, timeout=30)
            if p.returncode == 0 and p.stdout.strip():
                return lsblk_rows(json.loads(p.stdout))
        except Exception:  # noqa: BLE001 - an optional read; never block the scan
            continue
    return []


# --- Hardware Sniffer corrections -----------------------------------------------------------------------------------

def physical_cores(sys_cpu=SYS_CPU, cpuinfo="/proc/cpuinfo"):
    """Physical cores on all packages: the number Windows reports (NumberOfCores per CPU, summed), not logical CPUs.
    Each core is one distinct set of SMT siblings. None when it cannot be measured."""
    sets = set()
    try:
        for name in os.listdir(sys_cpu):
            if not (name.startswith("cpu") and name[3:].isdigit()):
                continue
            for leaf in ("core_cpus_list", "thread_siblings_list"):
                try:
                    with open(os.path.join(sys_cpu, name, "topology", leaf), encoding="ascii") as fh:
                        sets.add(fh.read().strip())
                    break
                except OSError:
                    continue
    except OSError:
        pass
    if sets:
        return len(sets)
    pairs = set()
    try:
        with open(cpuinfo, encoding="utf-8", errors="replace") as fh:
            for block in fh.read().split("\n\n"):
                info = dict(line.split(":", 1) for line in block.splitlines() if ":" in line)
                info = {k.strip(): v.strip() for k, v in info.items()}
                if "core id" in info:
                    pairs.add((info.get("physical id", "0"), info["core id"]))
    except OSError:
        return None
    return len(pairs) or None


def _cpu_list(text):
    out = []
    for part in (text or "").strip().split(","):
        if "-" in part:
            a, b = part.split("-", 1)
            out += range(int(a), int(b) + 1)
        elif part.strip():
            out.append(int(part))
    return out


def cpu_topology(sys_cpu=SYS_CPU):
    """The CPU layout from sysfs, in the shape hwcapture's Windows reading uses (packages, cores, SMT, hybrid)."""
    def read(cpu, leaf):
        try:
            with open(os.path.join(sys_cpu, cpu, "topology", leaf), encoding="ascii") as fh:
                return fh.read().strip()
        except OSError:
            return None
    cores, packages, dies = {}, {}, set()
    for name in sorted((n for n in os.listdir(sys_cpu) if n.startswith("cpu") and n[3:].isdigit()), key=lambda n: int(n[3:])):
        siblings = read(name, "core_cpus_list") or read(name, "thread_siblings_list")
        if siblings is None:
            continue   # offline
        package = read(name, "physical_package_id") or "0"
        dies.add((package, read(name, "die_id") or "0"))
        packages.setdefault(package, set()).add(int(name[3:]))
        cores.setdefault(siblings, {"lps": _cpu_list(siblings), "package": package})
    if not cores:
        raise OSError("no CPU topology in sysfs")
    # Intel hybrid parts expose two core PMUs; the cpus file lists which logical CPUs are which kind
    kinds = {}
    for pmu, kind in (("cpu_core", "performance"), ("cpu_atom", "efficiency")):
        try:
            with open(f"/sys/bus/event_source/devices/{pmu}/cpus", encoding="ascii") as fh:
                for lp in _cpu_list(fh.read()):
                    kinds[lp] = kind
        except OSError:
            pass
    core_map = [{"smt": len(c["lps"]) > 1, "logical": len(c["lps"]), "lps": c["lps"],
                 **({"kind": kinds[c["lps"][0]]} if c["lps"] and c["lps"][0] in kinds else {})} for c in cores.values()]
    order = sorted(packages)
    out = {"packages": len(packages), "dies": len(dies), "cores": len(core_map),
           "logical_processors": sum(c["logical"] for c in core_map), "hybrid": len(set(kinds.values())) > 1,
           "core_map": core_map, "package_map": [{"logical": len(packages[p]), "lps": sorted(packages[p])} for p in order],
           "logical_address_format": "Linux logical CPU number",
           "cores_per_package": [sum(1 for c in cores.values() if c["package"] == p) for p in order]}
    if out["hybrid"]:
        out["core_classes"] = [{"kind": k, "cores": sum(1 for c in core_map if c.get("kind") == k),
                                "logical": sum(c["logical"] for c in core_map if c.get("kind") == k)}
                               for k in ("efficiency", "performance") if k in kinds.values()]
    return out


def _models_under(device_dir):
    """Drive models below a controller's sysfs folder (the NVMe or SATA disks attached through it)."""
    found = []
    for root, _dirs, files in os.walk(device_dir):
        if "model" in files:
            try:
                with open(os.path.join(root, "model"), encoding="utf-8", errors="replace") as fh:
                    model = " ".join(fh.read().split())
            except OSError:
                continue
            if model and model not in found:
                found.append(model)
    return found


def _under(path, parents):
    return any(path == p or path.startswith(p + os.sep) for p in parents)


def raid_controllers(info, pci_devices=PCI_DEVICES):
    """RAID-mode controllers (PCI class 0104: Intel VMD, Intel RST and AMD RAID SATA) as Storage Controllers entries,
    and the PCI paths of the drive controllers behind them.

    The Sniffer's Linux collector lists only NVMe and SATA-class controllers, so with VMD on, Linux shows the NVMe drive
    on its own (on a VMD-made PCI domain) and nothing says VMD: the build went on to an EFI whose macOS can see no disk.
    On Windows the VMD/RST controller is what owns the drives, and the build stops on it with the BIOS fix."""
    entries, hidden = {}, set()
    roots = []
    for device in (getattr(info, "devices_by_class", None) or {}).get("RAID bus controller", []):
        slot = device.get("Device Path")
        if not slot:
            continue
        device_dir = os.path.join(pci_devices, slot)
        entry = {"Bus Type": device.get("Bus Type") or "PCI", "Device ID": device.get("Device ID")}
        for key in ("Subsystem ID", "PCI Path", "ACPI Path"):
            if device.get(key):
                entry[key] = device[key]
        drives = _models_under(device_dir) if os.path.isdir(device_dir) else []
        if drives:
            entry["Disk Drives"] = drives
        name = device.get("Name") or "RAID controller"
        key, n = name, 1
        while key in entries:
            key, n = f"{name}_#{n}", n + 1
        entries[key] = entry
        roots.append(os.path.realpath(device_dir))
    for kind in ("Non-Volatile memory controller", "SATA controller"):
        for device in (getattr(info, "devices_by_class", None) or {}).get(kind, []):
            slot = device.get("Device Path")
            if slot and device.get("PCI Path") and _under(os.path.realpath(os.path.join(pci_devices, slot)), roots):
                hidden.add(device["PCI Path"])
    return entries, hidden


# The builder's report schema (OpCore-Simplify report_validator): a device's ACPI path is under \_SB.
SB_PATH = re.compile(r"^[\\]?_SB(\.[A-Z0-9_]+)+$")


def patch_sniffer(info, pci_devices=PCI_DEVICES, cores=physical_cores):
    """Correct the Sniffer's Linux collector where it reports differently from its Windows one. Set on the instance,
    because hardware_collector looks each step up on the instance when it runs."""
    cpu, network, storage = info.cpu, info.network, info.storage_controllers
    raw_locate = info.get_device_location_paths

    def sb_only(device_dir):
        # Linux names every ACPI node, thermal-zone fans included (\_TZ_.FAN0); Windows gives a location path only to
        # devices under \_SB, and the builder rejects the whole report on any other ACPI path.
        found = raw_locate(device_dir)
        if "ACPI Path" in found and not SB_PATH.match(found["ACPI Path"]):
            found = {k: v for k, v in found.items() if k != "ACPI Path"}
        return found
    info.get_device_location_paths = sb_only

    def cpu_fixed():
        # Linux counted every /proc/cpuinfo entry, i.e. logical CPUs (20 on a 14-core i9-12900H); Windows reports
        # physical cores, and the build feeds this number to the AMD core-count kernel patches.
        out = cpu()
        n = cores()
        if isinstance(out, dict) and n:
            out["Core Count"] = str(n).zfill(2)
        return out

    def network_fixed():
        # The collector asks /sys/class/net/<if> for the PCI slot, which only its device/ link has, so every network
        # adapter came without the PCI and ACPI paths the Windows report carries.
        locate = info.get_device_location_paths

        def located(device_dir):
            found = locate(device_dir)
            device = os.path.join(device_dir, "device")
            if "PCI Path" not in found and os.path.isdir(device):
                merged = locate(device)
                merged.update(found)
                return merged
            return found
        info.get_device_location_paths = located
        try:
            return network()
        finally:
            info.get_device_location_paths = locate

    def storage_fixed():
        found = storage()
        raid, hidden = raid_controllers(info, pci_devices)
        if not raid:
            return found
        out = dict(raid)
        for name, props in (found or {}).items():
            if isinstance(props, dict) and props.get("PCI Path") in hidden:
                continue   # behind the RAID/VMD controller, as on Windows: its drives are listed on that controller
            out[name if name not in out else name + "_#1"] = props
        return out

    info.cpu, info.network, info.storage_controllers = cpu_fixed, network_fixed, storage_fixed
    return info


# --- the rest -------------------------------------------------------------------------------------------------------

def boot_epoch(stat="/proc/stat"):
    """When this kernel started (seconds since the epoch), from /proc/stat's btime; None if unknown."""
    try:
        with open(stat, encoding="ascii") as fh:
            for line in fh:
                if line.startswith("btime "):
                    return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    try:
        with open("/proc/uptime", encoding="ascii") as fh:
            return time.time() - float(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def invoking_user(env=None):
    """(uid, gid) of the person behind sudo or pkexec when running as root, else None."""
    env = os.environ if env is None else env
    if os.geteuid() != 0:
        return None
    for name in ("SUDO_UID", "PKEXEC_UID"):
        value = env.get(name, "")
        if value.isdigit() and int(value) != 0:
            uid = int(value)
            gid = env.get("SUDO_GID", "") if name == "SUDO_UID" else ""
            if not gid.isdigit():
                try:
                    import pwd  # noqa: PLC0415 - POSIX only
                    gid = pwd.getpwuid(uid).pw_gid
                except (ImportError, KeyError):
                    gid = uid
            return uid, int(gid)
    return None


def give_back(path, owner=None):
    """Hand a folder the root scan wrote back to the user who started it, so the build can run without root."""
    owner = owner or invoking_user()
    if not owner or not os.path.isdir(path):
        return False
    uid, gid = owner
    for root, dirs, files in os.walk(path):
        for name in [root] + [os.path.join(root, x) for x in dirs + files]:
            try:
                os.chown(name, uid, gid, follow_symlinks=False)
            except OSError:
                pass
    return True


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) == 2 else ""
    if command == "workdir":
        print(work_dir())
        sys.exit(0)
    if command == "acpi":
        state = acpi_readable()
        print({True: "readable", False: "needs root", None: "absent"}[state])
        sys.exit({True: 0, False: 1, None: 2}[state])
    sys.exit("usage: python3 -m p1401.linuxhost workdir|acpi")

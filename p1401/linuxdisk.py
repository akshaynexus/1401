"""The USB writer's Linux side: lists disks through lsblk, checks the picked stick again right before erasing it, and
erases it into the same layout p1401/rawdisk.py writes on Windows - an MBR with one active FAT32 partition (type 0x0C)
that starts at 1 MiB and is at most 16 GiB, labelled 1401 - with the standard tools: wipefs, sfdisk, mkfs.fat.

Safety mirrors usbwriter.eligible() and check_script(): only disks lsblk reports on the USB transport are offered; the
disk holding /, /boot, the EFI partition Linux uses, swap or any other mounted system path is refused, as are
read-only disks; a disk with an APFS, HFS+, Windows recovery, Linux root or encrypted/LVM partition stops the write so
the app can ask (1401-OS-PARTITIONS). The disk is found again by its path and checked against the serial, size, model
and id the user picked just before anything is written.

Everything that erases needs root (the writer is started through pkexec). Listing does not.

P1401_TEST_LOOP=/dev/loopN offers that one loop device as if it were a USB stick, so the whole write can be run against
a file-backed image. Only a device lsblk reports as TYPE loop is accepted there: it cannot be pointed at a real disk.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import time

MiB = 1 << 20
LABEL = "1401"
FAT32_LBA = "c"
TIMEOUT = 300
TEST_ENV = "P1401_TEST_LOOP"
BY_ID = "/dev/disk/by-id"
COLUMNS = "NAME,PATH,TRAN,RM,RO,HOTPLUG,SIZE,SERIAL,MODEL,VENDOR,TYPE,PTTYPE,FSTYPE,PARTTYPE,LABEL,MOUNTPOINTS"

# (program, what provides it). Searched on PATH and in the sbin folders, which a user's PATH can leave out.
TOOLS = [("lsblk", "util-linux"), ("sfdisk", "util-linux (on Debian and Ubuntu: fdisk)"), ("wipefs", "util-linux"),
         ("mkfs.fat", "dosfstools"), ("mount", "util-linux"), ("umount", "util-linux")]
SBIN = ("/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin")

# Partition types only an installed system (or somebody's data) has. EFI system and Microsoft basic data are left
# out on purpose, as on Windows: every Rufus, Ventoy or 1401 stick has them.
OS_GPT = {
    "7c3457ef-0000-11aa-aa11-00306543ecac": "Mac APFS",
    "48465300-0000-11aa-aa11-00306543ecac": "Mac HFS+",
    "de94bba4-06d1-4d40-a16a-bfd50179d6ac": "Windows recovery",
    "44479540-f297-41b2-9af7-d131d5f0458a": "Linux root (x86)",
    "4f68bce3-e8cd-4db1-96e7-fbcaf984b709": "Linux root (x86-64)",
    "69dad710-2ce4-4e3c-b16c-21a1d49abed3": "Linux root (ARM)",
    "b921b045-1df0-41c3-af44-4c6f280d3fae": "Linux root (ARM64)",
    "8484680c-9521-48c6-9c11-b0720656f69e": "Linux /usr (x86-64)",
    "b0e01050-ee5f-4390-949a-9101b17104e9": "Linux /usr (ARM64)",
}
OS_MBR = {"0xaf": "Mac HFS+", "0x27": "Windows recovery"}
OS_FS = {"apfs": "Mac APFS", "hfsplus": "Mac HFS+", "crypto_LUKS": "Linux encrypted volume",
         "LVM2_member": "Linux LVM volume"}

# Mount points that mean "the running system is on this disk".
SYSTEM_MOUNTS = {"/", "/usr", "/var", "/home", "/opt", "/srv", "/etc", "/nix", "/gnu", "/root", "[SWAP]"}
SYSTEM_TREES = ("/usr/", "/var/", "/opt/", "/srv/", "/etc/", "/nix/", "/gnu/")
BOOT_MOUNTS = {"/boot", "/efi"}
LIVE_TREES = ("/run/archiso", "/run/initramfs", "/run/live", "/lib/live", "/cdrom", "/run/rootfsbase", "/sysroot")


class DiskError(RuntimeError):
    pass


def tool(name):
    """Full path of a program, or None."""
    found = shutil.which(name)
    if found:
        return found
    for folder in SBIN:
        path = os.path.join(folder, name)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def missing_tools(find=None):
    """[(program, package)] for every program the write needs that is not installed."""
    find = find or tool
    return [(name, pkg) for name, pkg in TOOLS if not find(name)]


def is_root():
    return os.geteuid() == 0


def _run(cmd, input=None, check=True, timeout=TIMEOUT):
    """Runs a program by name (resolved like tool()). Returns the CompletedProcess; raises DiskError on failure."""
    exe = tool(cmd[0]) or cmd[0]
    try:
        p = subprocess.run([exe, *cmd[1:]], input=input, capture_output=True, text=True, errors="replace",
                           timeout=timeout)
    except FileNotFoundError:
        raise DiskError(f"{cmd[0]} is not installed") from None
    except subprocess.TimeoutExpired:
        raise DiskError(f"{cmd[0]} did not finish within {timeout} s") from None
    if check and p.returncode != 0:
        said = " ".join((p.stderr or p.stdout or "").split())[-600:]
        raise DiskError(f"{' '.join(cmd[:2])} failed (exit {p.returncode})" + (f": {said}" if said else ""))
    return p


# ---------------------------------------------------------------------------------------------------------- listing

def _bool(v):
    if isinstance(v, str):
        return v.strip() in ("1", "true", "True")
    return bool(v)


def _int(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _mounts(node):
    """Mount points of one lsblk node. lsblk before 2.37 has MOUNTPOINT (one) and no MOUNTPOINTS."""
    many = node.get("mountpoints")
    if many is None:
        many = [node.get("mountpoint")]
    return [m for m in many if m]


def lsblk(device=None):
    """lsblk's tree, as parsed JSON (a list of whole devices with "children")."""
    args = ["lsblk", "-J", "-b", "-o", COLUMNS] + ([device] if device else [])
    p = _run(args, check=False, timeout=60)
    if p.returncode != 0 and "MOUNTPOINTS" in (p.stderr or ""):
        p = _run(["lsblk", "-J", "-b", "-o", COLUMNS.replace("MOUNTPOINTS", "MOUNTPOINT")] + ([device] if device else []),
                 check=False, timeout=60)
    if p.returncode != 0:
        if device:
            return []  # the device is gone
        raise DiskError("lsblk failed: " + " ".join((p.stderr or "").split())[-300:])
    return json.loads(p.stdout or "{}").get("blockdevices") or []


def _descendants(node):
    for child in node.get("children") or []:
        yield child
        yield from _descendants(child)


def _mount_kind(mp):
    if mp in BOOT_MOUNTS or mp.startswith("/boot/") or mp.startswith("/efi/"):
        return "boot"
    if mp in SYSTEM_MOUNTS or mp.startswith(SYSTEM_TREES):
        return "system"
    if mp.startswith(LIVE_TREES):
        return "system"
    return None


def os_partitions(node):
    """['Mac APFS 12.3 GB', ...]: the partitions on this disk that hold a system or somebody's data volume."""
    found = []
    for part in _descendants(node):
        kind = (OS_GPT.get((part.get("parttype") or "").strip().lower())
                or OS_MBR.get((part.get("parttype") or "").strip().lower())
                or OS_FS.get(part.get("fstype") or ""))
        if kind:
            found.append(f"{kind} {_int(part.get('size')) / 1e9:.1f} GB")
    return found


def _by_id(folder=BY_ID):
    """{"/dev/sda": "usb-SanDisk_..."}: the most specific persistent name of each whole device."""
    best = {}
    rank = lambda n: (0 if n.startswith("usb-") else 1 if n.startswith(("ata-", "nvme-", "scsi-", "mmc-")) else 2, n)
    try:
        names = os.listdir(folder)
    except OSError:
        return best
    for name in names:
        if "-part" in name:
            continue
        dev = os.path.realpath(os.path.join(folder, name))
        if dev not in best or rank(name) < rank(best[dev]):
            best[dev] = name
    return best


def _backing_file(name):
    try:
        with open(f"/sys/block/{name}/loop/backing_file") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _natural(name):
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", name)]


def describe(node, ids=None, test=False):
    """One whole device as a disk dict in usbwriter's fields (Number is set by list_disks)."""
    ids = ids if ids is not None else {}
    path = node.get("path") or "/dev/" + (node.get("name") or "")
    mounts = _mounts(node) + [m for c in _descendants(node) for m in _mounts(c)]
    kinds = {m: _mount_kind(m) for m in mounts}
    style = {"gpt": "GPT", "dos": "MBR"}.get((node.get("pttype") or "").lower(), "RAW" if not node.get("pttype") else
                                                                               str(node.get("pttype")).upper())
    model = (node.get("model") or "").strip() or (node.get("vendor") or "").strip()
    disk = {"Number": -1, "FriendlyName": model, "SerialNumber": (node.get("serial") or "").strip(),
            "UniqueId": ids.get(path) or "", "BusType": "USB" if (node.get("tran") or "").lower() == "usb"
            else (node.get("tran") or "unknown").upper(), "Size": _int(node.get("size")),
            "IsBoot": "boot" in kinds.values(), "IsSystem": "system" in kinds.values(), "IsOffline": False,
            "IsReadOnly": _bool(node.get("ro")), "PartitionStyle": style, "Via": "linux", "Path": path,
            "Removable": _bool(node.get("rm")), "Mountpoints": mounts,
            "SystemMounts": [m for m, k in kinds.items() if k], "OsPartitions": os_partitions(node),
            "Partitions": [c.get("path") for c in node.get("children") or [] if c.get("type") == "part"],
            "Holders": [f"{c.get('path')} ({c.get('type')})" for c in _descendants(node)
                        if c.get("type") not in ("part", None)]}
    if not disk["UniqueId"]:
        disk["UniqueId"] = f"{model}:{disk['SerialNumber']}:{disk['Size']}"
    if test:
        backing = _backing_file(node.get("name") or "")
        disk.update(BusType="USB", Test=True, FriendlyName="1401 test loop device", UniqueId="loop:" + backing,
                    SerialNumber="")
    return disk


def list_disks(tree=None, env=None, ids=None):
    """Every whole disk (not zram, ram or loop devices), numbered in name order like Windows' PhysicalDrive numbers,
    plus the loop device named by P1401_TEST_LOOP (numbered after them)."""
    env = os.environ if env is None else env
    tree = lsblk() if tree is None else tree
    ids = _by_id() if ids is None else ids
    test = (env.get(TEST_ENV) or "").strip()
    disks, extra = [], []
    for node in sorted(tree, key=lambda n: _natural(n.get("name") or "")):
        name, kind = node.get("name") or "", node.get("type")
        if kind == "disk" and not name.startswith(("zram", "ram")):
            disks.append(describe(node, ids))
        elif kind == "loop" and test and (node.get("path") or "/dev/" + name) == test:
            extra.append(describe(node, ids, test=True))
    for i, d in enumerate(disks + extra):
        d["Number"] = i
    return disks + extra


def usb_only(disks):
    return [d for d in disks if d.get("BusType") == "USB"]


# --------------------------------------------------------------------------------------------- re-identify, erase

def current(disk, read=None):
    """The picked disk as it is now, found by its path and checked against what the user picked: same serial, id,
    size and model, still USB, still not the boot or system disk. Raises DiskError otherwise; nothing is erased."""
    read = read or (lambda path: lsblk(path))
    n, path = disk.get("Number"), disk.get("Path")
    tree = read(path) if path else []
    if not tree:
        raise DiskError(f"1401: disk {n} is not attached any more (the stick was unplugged or got a new name). "
                        "Nothing was erased. Press Refresh, pick the stick again and write.")
    ids = _by_id()
    now = describe(tree[0], ids, test=bool(disk.get("Test")))
    same = (now["Size"] == int(disk.get("Size") or 0) and now["BusType"] == "USB"
            and now["SerialNumber"] == (disk.get("SerialNumber") or "").strip()
            and now["FriendlyName"] == (disk.get("FriendlyName") or "").strip()
            and (not disk.get("UniqueId") or not ids or now["UniqueId"] == disk.get("UniqueId"))
            and not now["IsBoot"] and not now["IsSystem"])
    if not same:
        raise DiskError(f"1401: disk {n} is not the USB stick you picked any more. Nothing was erased.")
    now["Number"] = n
    return now


def check(disk, erase_os=False, read=None):
    """check_script's Linux twin: the disk is still the picked stick, and unless erase_os it holds no system."""
    now = current(disk, read)
    if now["Holders"]:
        raise DiskError(f"1401: disk {disk.get('Number')} has volumes open on it ({', '.join(now['Holders'])}). Close "
                        "them first (for example cryptsetup close, or vgchange -an for LVM) and write again. Nothing "
                        "was erased.")
    if not erase_os and now["OsPartitions"]:
        raise DiskError(f"1401-OS-PARTITIONS: disk {disk.get('Number')} holds {', '.join(now['OsPartitions'])}. "
                        "Nothing was erased.")
    return now


def _sector_size(path):
    try:
        with open(f"/sys/class/block/{os.path.basename(os.path.realpath(path))}/queue/logical_block_size") as fh:
            return int(fh.read().strip()) or 512
    except (OSError, ValueError):
        return 512


def sfdisk_script(size, part_cap, sector=512, label_id=None):
    """The layout rawdisk.wipe_mbr_fat32 writes: MBR, one active FAT32 (LBA) partition from 1 MiB, at most part_cap and
    leaving the last MiB free, rounded down to whole MiB."""
    length = min(part_cap, (size // MiB - 2) * MiB)
    if length <= 0:
        raise DiskError(f"the stick is too small ({size} bytes)")
    label_id = label_id if label_id is not None else (int.from_bytes(os.urandom(4), "little") or 1)
    return (f"label: dos\nlabel-id: 0x{label_id:08x}\nunit: sectors\n\n"
            f"start={MiB // sector}, size={length // sector}, type={FAT32_LBA}, bootable\n")


def _unmount_all(disk, say):
    """Unmounts every mounted partition of the disk, deepest first. A stick the desktop auto-mounted is the usual case."""
    tree = lsblk(disk["Path"])
    if not tree:
        return
    nodes = [tree[0]] + list(_descendants(tree[0]))
    count = 0
    for node in reversed(nodes):
        for mp in _mounts(node):
            if mp == "[SWAP]":
                raise DiskError(f"{node.get('path')} is in use as swap. Nothing was erased.")
            for attempt in range(5):
                if _run(["umount", mp], check=False, timeout=60).returncode == 0:
                    count += 1
                    break
                time.sleep(1)
            else:
                # something keeps a file open on it; the stick is about to be erased, so a lazy unmount loses nothing
                _run(["umount", "-l", mp], timeout=60)
                count += 1
                say(f"(something kept {mp} busy; detached it lazily)")
    say(f"OK unmounted {count} volume(s) on the stick")


def _settle(path):
    if tool("partprobe"):
        _run(["partprobe", path], check=False, timeout=60)
    elif tool("blockdev"):
        _run(["blockdev", "--rereadpt", path], check=False, timeout=60)
    if tool("udevadm"):
        _run(["udevadm", "settle", "--timeout=30"], check=False, timeout=60)


def _zero_ends(path, size):
    """Rufus zeroes the first and the last MiB (MBR/GPT header and the GPT backup); so does rawdisk.py."""
    zero = bytes(MiB)
    fd = os.open(path, os.O_WRONLY)
    try:
        for off in (0, (size // MiB - 1) * MiB):
            os.lseek(fd, off, os.SEEK_SET)
            if os.write(fd, zero) != MiB:
                raise DiskError(f"could not write to the stick at byte {off}")
        os.fsync(fd)
    finally:
        os.close(fd)


def _partition_of(path, tries=40, sleep=time.sleep):
    for _ in range(tries):
        tree = lsblk(path)
        parts = [c for c in (tree[0].get("children") or []) if c.get("type") == "part"] if tree else []
        if parts and os.path.exists(parts[0].get("path") or ""):
            return parts[0]["path"]
        sleep(0.5)
    raise DiskError("the stick was erased but Linux did not show its new partition. Unplug the stick, plug it in "
                    "again and write again.")


def erase_mbr_fat32(disk, part_cap, say=print):
    """Erases the picked disk and formats one FAT32 partition. Returns the partition's device path.
    disk["Path"] may be a regular file (an image) for tests: then nothing is unmounted or re-read, and mkfs.fat writes
    at the partition's offset inside the file. Returns (file, offset in bytes) in that case."""
    path, size = disk["Path"], int(disk["Size"])
    image = os.path.isfile(path)
    if not image:
        _unmount_all(disk, say)
        current(disk)  # nothing changed while the volumes went away
        for part in reversed(lsblk(path)[0].get("children") or []):
            _run(["wipefs", "-a", "-f", part["path"]], check=False)
    _run(["wipefs", "-a", "-f", path])
    _zero_ends(path, size)
    sector = 512 if image else _sector_size(path)
    _run(["sfdisk", "-q", "--wipe", "always", "--wipe-partitions", "always", path],
         input=sfdisk_script(size, part_cap, sector))
    length = min(part_cap, (size // MiB - 2) * MiB)
    if image:
        _run(["mkfs.fat", "-F", "32", "-n", LABEL, "--offset", str(MiB // 512), path, str(length // 1024)])
        say("OK erased the stick and made one FAT32 partition")
        return path, MiB
    _settle(path)
    part = _partition_of(path)
    _run(["wipefs", "-a", "-f", part], check=False)
    _run(["mkfs.fat", "-F", "32", "-n", LABEL, part])
    _settle(path)
    say("OK erased the stick and made one FAT32 partition")
    return part


def mount(part):
    """Mounts the new FAT32 partition on a fresh private folder. Returns the folder."""
    where = tempfile.mkdtemp(prefix="1401-stick-")
    try:
        _run(["mount", "-t", "vfat", "-o", "rw,nosuid,nodev,flush", part, where], timeout=60)
    except DiskError:
        os.rmdir(where)
        raise
    return where


def unmount(where, say=print, part=None):
    """Flushes everything to the stick and unmounts it, so it can be pulled out. With part, also any other mount of
    the new partition (a desktop that auto-mounted it after the format)."""
    os.sync()
    for _ in range(10):
        if _run(["umount", where], check=False, timeout=120).returncode == 0:
            break
        time.sleep(1)
    else:
        raise DiskError(f"the stick is written but could not be unmounted from {where}; run 'sync' and 'umount {where}' "
                        "before unplugging it")
    try:
        os.rmdir(where)
    except OSError:
        pass
    if part and not os.path.isfile(part):
        for node in lsblk(part):
            for mp in _mounts(node):
                _run(["umount", mp], check=False, timeout=120)
    say("OK unmounted the stick; it can be unplugged")


def give_back(path):
    """Files the root writer made in the user's build folder (.cache) are handed back to the user who ran pkexec."""
    uid = os.environ.get("PKEXEC_UID") or os.environ.get("SUDO_UID")
    if not uid or not uid.isdigit() or not os.path.exists(path):
        return
    import pwd  # noqa: PLC0415
    try:
        gid = pwd.getpwuid(int(uid)).pw_gid
    except KeyError:
        gid = int(uid)
    for folder, dirs, files in os.walk(path):
        for name in [folder] + [os.path.join(folder, n) for n in dirs + files]:
            try:
                os.lchown(name, int(uid), gid)
            except OSError:
                pass

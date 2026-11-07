"""Which disks and mount points the window needs to know about on Linux, read without root: lsblk and findmnt from
util-linux. Windows reads the same through DriveInfo (removable drives, the drive 1401 runs from)."""
import json
import os
import subprocess


def _run(cmd, timeout=15):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.stdout if p.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def lsblk_tree(text=None):
    """lsblk's device tree (disks with their partitions as children)."""
    if text is None:
        text = _run(["lsblk", "-J", "-b", "-o", "NAME,PATH,TYPE,TRAN,RM,HOTPLUG,MOUNTPOINTS"])
    try:
        return json.loads(text).get("blockdevices") or []
    except (ValueError, AttributeError):
        return []


def _mountpoints(node):
    points = node.get("mountpoints")
    if points is None:   # older lsblk: a single "mountpoint"
        points = [node.get("mountpoint")]
    return [p for p in points if p and p.startswith("/")]


def _flag(v):
    return v is True or v in ("1", 1, "true")


def disk_mounts(tree=None):
    """{/dev/<disk>: [mount points of the disk and its partitions]}."""
    out = {}
    for disk in lsblk_tree() if tree is None else tree:
        if disk.get("type") != "disk":
            continue
        points = _mountpoints(disk)
        stack = list(disk.get("children") or [])
        while stack:
            child = stack.pop(0)
            points += _mountpoints(child)
            stack += child.get("children") or []
        out[disk.get("path") or "/dev/" + disk.get("name", "")] = points
    return out


def removable_roots(tree=None):
    """Mount points on USB or removable disks: what Windows calls removable drives (stick logs, NullMoth folders)."""
    roots = []
    mounts = disk_mounts(tree)
    for disk in lsblk_tree() if tree is None else tree:
        if disk.get("type") != "disk":
            continue
        if disk.get("tran") == "usb" or _flag(disk.get("rm")) or _flag(disk.get("hotplug")):
            roots += [p for p in mounts.get(disk.get("path") or "/dev/" + disk.get("name", ""), []) if p != "/"]
    return roots


def disks_under(path):
    """The /dev nodes of the whole disks a path is stored on (through partitions, LVM, LUKS, RAID)."""
    source = _run(["findmnt", "-n", "-o", "SOURCE", "--target", path]).strip()
    if not source:
        return set()
    source = source.split("[", 1)[0]   # btrfs: /dev/nvme0n1p2[/@home]
    if not source.startswith("/dev/"):
        return set()
    out = set()
    for line in _run(["lsblk", "-n", "-s", "-r", "-p", "-o", "NAME,TYPE", source]).splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == "disk":
            out.add(parts[0])
    return out


def mount_root(path):
    """The mount point a path is on (Windows: Path.GetPathRoot)."""
    path = os.path.abspath(path)
    while not os.path.ismount(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return path

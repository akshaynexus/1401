"""The Linux USB writer (p1401/linuxdisk.py and usbwriter's Linux branch), against canned lsblk output and mocked
commands. Nothing here touches a disk."""
import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from p1401 import linuxdisk, usbwriter

GiB = 1 << 30
STICK_SERIAL = "04019a2da168e0b8"
STICK_ID = "usb-USB_SanDisk_3.2Gen1_04019a2da168e0b8-0:0"


def node(name, size, tran=None, kind="disk", children=None, **kw):
    n = {"name": name, "path": "/dev/" + name, "tran": tran, "rm": kw.pop("rm", False), "ro": kw.pop("ro", False),
         "hotplug": False, "size": size, "serial": kw.pop("serial", None), "model": kw.pop("model", None),
         "vendor": None, "type": kind, "pttype": kw.pop("pttype", None), "fstype": kw.pop("fstype", None),
         "parttype": kw.pop("parttype", None), "label": None, "mountpoints": kw.pop("mountpoints", [])}
    if children:
        n["children"] = children
    n.update(kw)
    return n


def stick(children=None, **kw):
    fields = {"size": 61530439680, "tran": "usb", "rm": True, "serial": STICK_SERIAL, "model": "SanDisk 3.2Gen1",
              "pttype": "dos", **kw}
    size, tran = fields.pop("size"), fields.pop("tran")
    return node("sda", size, tran, children=children if children is not None else [
        node("sda1", 61530423296, kind="part", pttype="dos", fstype="vfat", parttype="0xc",
             mountpoints=["/run/media/akshay/02AB-7A20"])], **fields)


NVME = node("nvme0n1", 1024209543168, "nvme", serial="S6W6", model="SAMSUNG MZVL21T0HCLR-00BTW", pttype="gpt", children=[
    node("nvme0n1p1", 536870912, "nvme", kind="part", fstype="vfat", parttype="c12a7328-f81f-11d2-ba4b-00a0c93ec93b",
         mountpoints=["/boot/efi"]),
    node("nvme0n1p2", 1023670538240, "nvme", kind="part", fstype="btrfs", parttype="0fc63daf-8483-4772-8e79-3d69d8477de4",
         mountpoints=["/var/log", "/home", "/"])])
ZRAM = node("zram0", 33283899392, fstype="swap", mountpoints=["[SWAP]"])
LOOP = node("loop0", 3 * GiB, kind="loop")
IDS = {"/dev/sda": STICK_ID, "/dev/nvme0n1": "nvme-SAMSUNG_MZVL21T0HCLR-00BTW_S6W6"}


def listed(tree, env=None):
    return linuxdisk.list_disks(tree=copy.deepcopy(tree), env=env or {}, ids=IDS)


class Listing(unittest.TestCase):
    def test_disks_are_numbered_in_name_order_without_zram(self):
        disks = listed([stick(), ZRAM, NVME])
        self.assertEqual([(d["Number"], d["Path"]) for d in disks], [(0, "/dev/nvme0n1"), (1, "/dev/sda")])

    def test_a_usb_stick_has_windows_fields_and_is_eligible(self):
        d = listed([stick(), NVME])[1]
        self.assertEqual((d["BusType"], d["FriendlyName"], d["SerialNumber"], d["UniqueId"], d["Size"], d["Via"],
                          d["PartitionStyle"], d["IsBoot"], d["IsSystem"], d["IsReadOnly"], d["IsOffline"]),
                         ("USB", "SanDisk 3.2Gen1", STICK_SERIAL, STICK_ID, 61530439680, "linux", "MBR", False, False,
                          False, False))
        self.assertEqual(d["Mountpoints"], ["/run/media/akshay/02AB-7A20"])
        self.assertEqual(usbwriter.eligible(d), (True, []))

    def test_the_disk_linux_runs_from_is_refused_with_its_mounts(self):
        d = listed([NVME])[0]
        ok, why = usbwriter.eligible(d, allow_large=True)
        self.assertFalse(ok)
        self.assertTrue(d["IsBoot"] and d["IsSystem"])
        self.assertTrue(any("not a USB disk (bus: NVME)" in w for w in why))
        self.assertTrue(any("booted from" in w and "/boot/efi" in w for w in why))
        self.assertTrue(any("running system" in w and "/home" in w for w in why))

    def test_a_usb_disk_holding_root_or_boot_is_refused(self):
        for mp in ("/", "/boot", "/boot/efi", "/efi", "/usr", "/var/lib", "[SWAP]", "/run/archiso/bootmnt"):
            with self.subTest(mp=mp):
                tree = [stick([node("sda1", 30 * GiB, kind="part", fstype="ext4", mountpoints=[mp])])]
                ok, why = usbwriter.eligible(listed(tree)[0])
                self.assertFalse(ok, why)

    def test_root_on_an_encrypted_volume_on_a_usb_disk_is_refused(self):
        crypt = node("luks-1", 30 * GiB, kind="crypt", fstype="btrfs", mountpoints=["/"])
        tree = [stick([node("sda2", 30 * GiB, kind="part", fstype="crypto_LUKS", children=[crypt])])]
        self.assertFalse(usbwriter.eligible(listed(tree)[0])[0])

    def test_ordinary_mounts_do_not_refuse(self):
        for mp in ("/run/media/me/STICK", "/media/STICK", "/mnt/usb", "/home/me/stick"):
            with self.subTest(mp=mp):
                tree = [stick([node("sda1", 30 * GiB, kind="part", fstype="vfat", mountpoints=[mp])])]
                self.assertTrue(usbwriter.eligible(listed(tree)[0])[0])

    def test_read_only_small_and_huge_disks(self):
        self.assertTrue(any("read-only" in w for w in usbwriter.eligible(listed([stick(ro=True)])[0])[1]))
        self.assertTrue(any("too small" in w for w in usbwriter.eligible(listed([stick(size=GiB)])[0])[1]))
        big = listed([stick(size=2 * 10**12)])[0]
        self.assertFalse(usbwriter.eligible(big)[0])
        self.assertTrue(usbwriter.eligible(big, allow_large=True)[0])

    def test_old_lsblk_strings_and_single_mountpoint(self):
        old = stick([{"name": "sda1", "path": "/dev/sda1", "type": "part", "size": "100", "mountpoint": "/"}],
                    ro="0", rm="1", size="61530439680")
        del old["mountpoints"]
        d = listed([old])[0]
        self.assertEqual((d["Size"], d["IsReadOnly"], d["Removable"], d["IsSystem"]), (61530439680, False, True, True))

    def test_loop_devices_are_hidden_unless_named_for_a_test(self):
        self.assertEqual([d["Path"] for d in listed([stick(), LOOP])], ["/dev/sda"])
        disks = listed([stick(), LOOP], env={linuxdisk.TEST_ENV: "/dev/loop0"})
        loop = disks[-1]
        self.assertEqual((loop["Path"], loop["BusType"], loop["Test"], loop["Number"]), ("/dev/loop0", "USB", True, 1))
        self.assertTrue(usbwriter.eligible(loop)[0])

    def test_the_test_override_cannot_point_at_a_real_disk(self):
        for target in ("/dev/sda", "/dev/nvme0n1"):
            disks = listed([NVME, node("sdb", 32 * GiB, "sata", model="Internal")], env={linuxdisk.TEST_ENV: target})
            self.assertFalse(any(d.get("Test") for d in disks))
            self.assertTrue(all(d["BusType"] != "USB" for d in disks))

    def test_usb_only(self):
        self.assertEqual([d["Path"] for d in linuxdisk.usb_only(listed([stick(), NVME]))], ["/dev/sda"])


class OsPartitions(unittest.TestCase):
    def kinds(self, parts):
        return linuxdisk.os_partitions({"children": parts})

    def test_system_partitions_are_named(self):
        cases = {"7c3457ef-0000-11aa-aa11-00306543ecac": "Mac APFS", "48465300-0000-11aa-aa11-00306543ecac": "Mac HFS+",
                 "DE94BBA4-06D1-4D40-A16A-BFD50179D6AC": "Windows recovery",
                 "4f68bce3-e8cd-4db1-96e7-fbcaf984b709": "Linux root (x86-64)"}
        for guid, word in cases.items():
            with self.subTest(guid=guid):
                self.assertEqual(self.kinds([{"parttype": guid, "size": 12.3e9}]), [word + " 12.3 GB"])
        self.assertEqual(self.kinds([{"parttype": "0xaf", "size": 1e9}]), ["Mac HFS+ 1.0 GB"])
        self.assertEqual(self.kinds([{"fstype": "apfs", "size": 1e9}]), ["Mac APFS 1.0 GB"])
        self.assertEqual(self.kinds([{"fstype": "crypto_LUKS", "size": 1e9}]), ["Linux encrypted volume 1.0 GB"])

    def test_efi_basic_data_and_plain_linux_data_are_fine(self):
        self.assertEqual(self.kinds([
            {"parttype": "c12a7328-f81f-11d2-ba4b-00a0c93ec93b", "fstype": "vfat", "size": 1},
            {"parttype": "e3c9e316-0b5c-4db8-817d-f92df00215ae", "size": 1},
            {"parttype": "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7", "fstype": "exfat", "size": 1},
            {"parttype": "0fc63daf-8483-4772-8e79-3d69d8477de4", "fstype": "ext4", "size": 1},
            {"parttype": "0xc", "fstype": "vfat", "size": 1}]), [])


class ReIdentify(unittest.TestCase):
    def setUp(self):
        self.picked = listed([stick(), NVME])[1]
        patcher = patch.object(linuxdisk, "_by_id", return_value=IDS)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_same_stick_passes(self):
        now = linuxdisk.check(self.picked, read=lambda p: [stick()])
        self.assertEqual(now["Number"], 1)

    def test_another_stick_at_the_same_path_is_refused(self):
        for change in ({"serial": "OTHER"}, {"size": 32 * GiB}, {"model": "Kingston"}, {"tran": "sata"}):
            with self.subTest(change=change):
                with self.assertRaisesRegex(linuxdisk.DiskError, "not the USB stick you picked any more. Nothing was erased"):
                    linuxdisk.check(self.picked, read=lambda p: [stick(**change)])

    def test_a_different_persistent_id_is_refused(self):
        with patch.object(linuxdisk, "_by_id", return_value={"/dev/sda": "usb-Other_X-0:0"}):
            with self.assertRaisesRegex(linuxdisk.DiskError, "not the USB stick you picked"):
                linuxdisk.check(self.picked, read=lambda p: [stick()])

    def test_a_stick_that_now_holds_the_system_is_refused(self):
        tree = [stick([node("sda1", GiB, kind="part", mountpoints=["/"])])]
        with self.assertRaisesRegex(linuxdisk.DiskError, "not the USB stick you picked"):
            linuxdisk.check(self.picked, read=lambda p: tree)

    def test_a_gone_stick_says_so(self):
        with self.assertRaisesRegex(linuxdisk.DiskError, "not attached any more.*Nothing was erased"):
            linuxdisk.check(self.picked, read=lambda p: [])

    def test_os_partitions_stop_with_the_marker_the_app_asks_about(self):
        tree = [stick([node("sda1", 12_300_000_000, kind="part", parttype="7c3457ef-0000-11aa-aa11-00306543ecac")],
                      pttype="gpt")]
        with self.assertRaises(linuxdisk.DiskError) as e:
            linuxdisk.check(self.picked, read=lambda p: tree)
        self.assertIn("1401-OS-PARTITIONS: disk 1 holds Mac APFS 12.3 GB. Nothing was erased.", str(e.exception))
        linuxdisk.check(self.picked, erase_os=True, read=lambda p: tree)  # confirmed a second time: allowed

    def test_open_crypt_or_lvm_volumes_stop_even_when_confirmed(self):
        tree = [stick([node("sda1", GiB, kind="part", fstype="crypto_LUKS",
                            children=[node("luks-x", GiB, kind="crypt", fstype="ext4")])])]
        with self.assertRaisesRegex(linuxdisk.DiskError, "volumes open on it.*luks-x"):
            linuxdisk.check(self.picked, erase_os=True, read=lambda p: tree)


class Layout(unittest.TestCase):
    def test_mirrors_rawdisk_mbr_one_active_fat32_partition(self):
        s = linuxdisk.sfdisk_script(61530439680, usbwriter.PART_CAP, 512, label_id=0x1234ABCD)
        self.assertIn("label: dos\n", s)
        self.assertIn("label-id: 0x1234abcd\n", s)
        self.assertIn(f"start=2048, size={usbwriter.PART_CAP // 512}, type=c, bootable", s)

    def test_a_small_stick_leaves_the_last_mib_free(self):
        s = linuxdisk.sfdisk_script(3 * GiB, usbwriter.PART_CAP, 512)
        self.assertIn(f"size={(3072 - 2) * 2048}, ", s)

    def test_4k_sectors(self):
        s = linuxdisk.sfdisk_script(64 * GiB, usbwriter.PART_CAP, 4096)
        self.assertIn(f"start=256, size={usbwriter.PART_CAP // 4096}, ", s)


class Erase(unittest.TestCase):
    """The command sequence on a block device, with every command recorded instead of run."""

    def test_unmount_wipe_partition_format_in_that_order(self):
        calls = []
        tree_before = [stick()]
        tree_after = [stick([node("sda1", usbwriter.PART_CAP, kind="part")])]
        state = {"tree": tree_before}

        def run(cmd, input=None, check=True, timeout=0):
            calls.append((cmd, input))
            if cmd[0] == "umount":
                state["tree"] = [stick([node("sda1", 61530423296, kind="part", fstype="vfat")])]
            if cmd[0] == "sfdisk":
                state["tree"] = tree_after
            return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        disk = listed([stick()])[0]
        with patch.object(linuxdisk, "_run", run), patch.object(linuxdisk, "lsblk", lambda dev=None: copy.deepcopy(state["tree"])), \
                patch.object(linuxdisk, "_by_id", return_value=IDS), patch.object(linuxdisk, "_zero_ends") as zero, \
                patch.object(linuxdisk, "_sector_size", return_value=512), patch.object(linuxdisk.os.path, "exists", return_value=True), \
                patch.object(linuxdisk, "tool", lambda n: "/usr/bin/" + n), patch.object(linuxdisk.os.path, "isfile", return_value=False):
            part = linuxdisk.erase_mbr_fat32(disk, usbwriter.PART_CAP, say=lambda m: None)
        self.assertEqual(part, "/dev/sda1")
        names = [c[0][0] for c in calls]
        self.assertEqual(names[0], "umount")
        self.assertEqual(calls[0][0], ["umount", "/run/media/akshay/02AB-7A20"])
        self.assertLess(names.index("wipefs"), names.index("sfdisk"))
        self.assertLess(names.index("sfdisk"), names.index("mkfs.fat"))
        zero.assert_called_once_with("/dev/sda", 61530439680)
        sfdisk = next(c for c in calls if c[0][0] == "sfdisk")
        self.assertEqual(sfdisk[0][-1], "/dev/sda")
        self.assertIn("type=c, bootable", sfdisk[1])
        self.assertIn(["mkfs.fat", "-F", "32", "-n", "1401", "/dev/sda1"], [c[0] for c in calls])
        # every destructive command names the picked disk or its partition, nothing else
        for cmd, _ in calls:
            if cmd[0] in ("wipefs", "sfdisk", "mkfs.fat"):
                self.assertTrue(cmd[-1].startswith("/dev/sda"), cmd)

    def test_an_image_file_is_formatted_at_the_partition_offset(self):
        with tempfile.TemporaryDirectory() as tmp:
            img = os.path.join(tmp, "stick.img")
            with open(img, "wb") as fh:
                fh.truncate(3 * GiB)
            calls = []
            with patch.object(linuxdisk, "_run", lambda cmd, input=None, **k: calls.append(cmd)):
                got = linuxdisk.erase_mbr_fat32({"Path": img, "Size": 3 * GiB}, usbwriter.PART_CAP, say=lambda m: None)
            self.assertEqual(got, (img, 1 << 20))
            self.assertIn(["mkfs.fat", "-F", "32", "-n", "1401", "--offset", "2048", img, str(3070 * 1024)], calls)
            with open(img, "rb") as fh:
                self.assertEqual(fh.read(16), bytes(16))  # the first MiB was zeroed


class Preconditions(unittest.TestCase):
    def test_not_root_stops_and_names_pkexec(self):
        with patch.object(linuxdisk, "is_root", return_value=False):
            stop = usbwriter._linux_preconditions()
        self.assertTrue(stop.startswith("STOP 1401-NEEDS-ROOT:"))
        self.assertIn("pkexec", stop)
        self.assertIn("Nothing was erased", stop)

    def test_missing_tools_name_their_package(self):
        with patch.object(linuxdisk, "is_root", return_value=True), \
                patch.object(linuxdisk, "tool", lambda n: None if n in ("mkfs.fat", "sfdisk") else "/usr/bin/" + n):
            stop = usbwriter._linux_preconditions()
        self.assertTrue(stop.startswith("STOP 1401-MISSING-TOOLS:"))
        self.assertIn("mkfs.fat (install the package dosfstools)", stop)
        self.assertIn("sfdisk (install the package util-linux", stop)

    def test_ready(self):
        with patch.object(linuxdisk, "is_root", return_value=True), patch.object(linuxdisk, "tool", lambda n: "/x/" + n):
            self.assertIsNone(usbwriter._linux_preconditions())

    def test_cli_write_stops_before_listing_when_not_root(self):
        out = io.StringIO()
        with patch.object(usbwriter, "_LINUX", True), patch.object(linuxdisk, "is_root", return_value=False), \
                patch.object(usbwriter, "list_disks") as lister, redirect_stdout(out):
            rc = usbwriter.cli_write(["1", "efi", "24"])
        self.assertEqual(rc, 1)
        lister.assert_not_called()
        self.assertIn("1401-NEEDS-ROOT", out.getvalue())


class CliWrite(unittest.TestCase):
    """cli_write on Linux prints the same STEP/OK/PROGRESS/STOP lines as on Windows, and unmounts the stick."""

    def run_cli(self, argv, write_result=None, write_error=None):
        disks = listed([stick(), NVME])
        written, unmounted = [], []

        def fake_write(disk, efi, info, **kw):
            written.append(disk["Path"])
            if write_error:
                raise write_error
            return dict(write_result or {"root": "/tmp/1401-stick-x", "linux_mount": "/tmp/1401-stick-x",
                                         "device": disk["Path"], "nullmoth": None})

        out = io.StringIO()
        with patch.object(usbwriter, "_LINUX", True), patch.object(usbwriter, "_linux_preconditions", return_value=None), \
                patch.object(usbwriter, "list_disks", return_value=disks), \
                patch.object(usbwriter, "write", fake_write), \
                patch("p1401.machine_handoff.verified", return_value=None), \
                patch("p1401.netsupport.needs_local_image", return_value=False), \
                patch("p1401.apple.local_image", return_value={"local": True, "name": "local image"}), \
                patch.object(linuxdisk, "unmount", lambda where, say=print, part=None: unmounted.append(where)), \
                redirect_stdout(out):
            rc = usbwriter.cli_write(argv)
        return rc, out.getvalue(), written, unmounted

    def test_by_number(self):
        rc, out, written, unmounted = self.run_cli(["1", "efi", "24", "--stick-id", STICK_ID, "--stick-size",
                                                    "61530439680", "--stick-name", "SanDisk 3.2Gen1"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(written, ["/dev/sda"])
        self.assertEqual(unmounted, ["/tmp/1401-stick-x"])
        self.assertIn("STEP erasing and writing disk 1 (SanDisk 3.2Gen1)", out)
        self.assertIn("OK the stick is ready at /dev/sda", out)

    def test_by_device_path(self):
        rc, out, written, _ = self.run_cli(["/dev/sda", "efi", "24"])
        self.assertEqual((rc, written), (0, ["/dev/sda"]), out)

    def test_the_system_disk_is_refused_by_number_and_by_path(self):
        for first in ("0", "/dev/nvme0n1"):
            with self.subTest(first=first):
                rc, out, written, _ = self.run_cli([first, "efi", "24"])
                self.assertEqual((rc, written), (1, []))
                self.assertIn("STOP not a USB disk", out)

    def test_a_moved_stick_is_found_by_its_id(self):
        rc, out, written, _ = self.run_cli(["0", "efi", "24", "--stick-id", STICK_ID, "--stick-size", "61530439680",
                                            "--stick-name", "SanDisk 3.2Gen1"])
        self.assertEqual((rc, written), (0, ["/dev/sda"]), out)
        self.assertIn("OK the stick is disk 1 now (it was disk 0 when it was picked)", out)

    def test_a_gone_path_is_renumbered(self):
        rc, out, written, _ = self.run_cli(["/dev/sdz", "efi", "24"])
        self.assertEqual((rc, written), (1, []))
        self.assertIn("STOP 1401-DISK-RENUMBERED", out)

    def test_a_failed_write_after_mount_is_not_reported_ready(self):
        with self.assertRaises(usbwriter.UsbError):
            self.run_cli(["1", "efi", "24"], write_error=usbwriter.UsbError("1401-OS-PARTITIONS: disk 1 holds Mac APFS"))


class WriteLinux(unittest.TestCase):
    """usbwriter.write's Linux branch: check, erase, mount, payload; unmount when the payload fails."""

    def test_order_and_unmount_on_failure(self):
        disk = listed([stick()])[0]
        steps = []
        with patch("p1401.validate.validate", return_value={"ok": True}), \
                patch.object(linuxdisk, "check", lambda d, e: steps.append(("check", e))), \
                patch.object(linuxdisk, "erase_mbr_fat32", lambda d, cap, say: steps.append("erase") or "/dev/sda1"), \
                patch.object(linuxdisk, "mount", lambda p: steps.append(("mount", p)) or "/tmp/m"), \
                patch.object(linuxdisk, "unmount", lambda w, say=print, part=None: steps.append(("unmount", w, part))), \
                patch.object(usbwriter, "_write_payload", side_effect=OSError(5, "I/O error")), \
                patch("builtins.print"):
            with self.assertRaises(OSError):
                usbwriter.write(disk, "efi", {}, erase_os=True)
        self.assertEqual(steps, [("check", True), "erase", ("mount", "/dev/sda1"), ("unmount", "/tmp/m", "/dev/sda1")])

    def test_disk_errors_become_usb_errors_with_the_marker(self):
        disk = listed([stick()])[0]
        with patch("p1401.validate.validate", return_value={"ok": True}), \
                patch.object(linuxdisk, "check", side_effect=linuxdisk.DiskError("1401-OS-PARTITIONS: disk 1 holds x")), \
                patch.object(linuxdisk, "erase_mbr_fat32") as erase:
            with self.assertRaisesRegex(usbwriter.UsbError, "1401-OS-PARTITIONS"):
                usbwriter.write(disk, "efi", {})
        erase.assert_not_called()

    def test_windows_disks_never_take_the_linux_branch(self):
        self.assertFalse(usbwriter._LINUX)  # importing the module keeps Windows' writer
        win = {"Number": 3, "BusType": "USB", "Size": 16 * 10**9}
        with patch("p1401.validate.validate", return_value={"ok": True}), \
                patch.object(usbwriter, "_ps", side_effect=usbwriter.UsbError("windows path")), \
                patch.object(linuxdisk, "check") as check:
            with self.assertRaisesRegex(usbwriter.UsbError, "windows path"):
                usbwriter.write(win, "efi", {})
        check.assert_not_called()


class ListCli(unittest.TestCase):
    def test_list_prints_usb_disks_with_reasons(self):
        out = io.StringIO()
        with patch.object(usbwriter, "_LINUX", True), patch.object(usbwriter, "list_disks", return_value=listed([stick(), NVME])), \
                redirect_stdout(out):
            usbwriter.cli_list([])
        shown = json.loads(out.getvalue())
        self.assertEqual([(d["Path"], d["Eligible"], d["Reasons"]) for d in shown], [("/dev/sda", True, [])])

    def test_list_all(self):
        out = io.StringIO()
        with patch.object(usbwriter, "_LINUX", True), patch.object(usbwriter, "list_disks", return_value=listed([stick(), NVME])), \
                redirect_stdout(out):
            usbwriter.cli_list(["--all"])
        self.assertEqual([d["Eligible"] for d in json.loads(out.getvalue())], [False, True])


class ReadBack(unittest.TestCase):
    def test_linux_readback_drops_cached_pages_and_reads_everything(self):
        from p1401 import apple
        with tempfile.NamedTemporaryFile(delete=False) as fh:
            fh.write(os.urandom(3 << 20))
            name = fh.name
        self.addCleanup(os.remove, name)
        advised = []
        real = getattr(os, "posix_fadvise", None)
        if real is None or not apple.sys.platform.startswith("linux"):
            self.skipTest("posix_fadvise is Linux-only")
        with patch.object(apple.os, "posix_fadvise", lambda *a: advised.append(a[1:]) or real(*a)):
            data = b"".join(apple._read_uncached(name, block=1 << 20))
        with open(name, "rb") as fh:
            self.assertEqual(data, fh.read())
        self.assertIn((0, 0, os.POSIX_FADV_DONTNEED), advised)


if __name__ == "__main__":
    unittest.main()

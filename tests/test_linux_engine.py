"""The Linux host path of Check this PC: ACPI tables from sysfs (root only), drive models from lsblk, the working-files
folder, the boot time, and the scan controller's root handling. Every test builds its own fake sysfs, so they run the
same on any OS and as any user."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from p1401 import linuxhost, scan, scan_evidence as se


def table(sig, length=40):
    return sig.encode("ascii").ljust(4, b"!")[:4] + length.to_bytes(4, "little") + b"\0" * (length - 8)


class Tmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)


class WorkDir(unittest.TestCase):
    def test_xdg_data_home_is_used(self):
        self.assertEqual(linuxhost.work_dir({"XDG_DATA_HOME": "/x/data", "HOME": "/home/u"}), "/x/data/NullMoth/1401")

    def test_unset_or_relative_xdg_falls_back_to_local_share(self):
        for env in ({"HOME": "/home/u"}, {"HOME": "/home/u", "XDG_DATA_HOME": "rel/path"}, {"HOME": "/home/u", "XDG_DATA_HOME": ""}):
            self.assertEqual(linuxhost.work_dir(env), "/home/u/.local/share/NullMoth/1401")


class AcpiDump(Tmp):
    def sysfs(self):
        src = self.root / "tables"
        (src / "dynamic").mkdir(parents=True)
        (src / "data").mkdir()
        for name, sig in (("DSDT", "DSDT"), ("FACP", "FACP"), ("SSDT1", "SSDT"), ("SSDT12", "SSDT"), ("ASF!", "ASF!")):
            (src / name).write_bytes(table(sig))
        (src / "FACS").write_bytes(b"FACS" + (64).to_bytes(4, "little") + b"\0" * 56)
        (src / "BAD").write_bytes(b"BAD!")                       # shorter than any table header
        (src / "dynamic" / "SSDT20").write_bytes(table("SSDT"))  # loaded at run time: not in a Windows dump
        (src / "data" / "BERT").write_bytes(b"x" * 64)
        return src

    def test_static_tables_are_copied_lower_case_like_acpidump(self):
        src = self.sysfs()
        dest = self.root / "out" / "ACPI"
        dest.mkdir(parents=True)
        (dest / "stale.aml").write_bytes(b"old")
        names = linuxhost.dump_acpi_tables(str(dest), str(src))
        self.assertEqual(sorted(names), ["asf!.aml", "dsdt.aml", "facp.aml", "facs.aml", "ssdt1.aml", "ssdt12.aml"])
        self.assertEqual(sorted(os.listdir(dest)), sorted(names))   # the stale file is gone, no dynamic/ or data/
        self.assertEqual((dest / "dsdt.aml").read_bytes(), table("DSDT"))
        self.assertEqual(scan.dsdt_in(str(dest)), "dsdt.aml")

    def test_unreadable_tables_raise_the_root_message(self):
        src = self.sysfs()
        real = os.access
        with mock.patch.object(linuxhost.os, "access", lambda p, m: False if str(p).startswith(str(src)) else real(p, m)):
            self.assertIs(linuxhost.acpi_readable(str(src)), False)
            with self.assertRaises(linuxhost.AcpiAccessError) as e:
                linuxhost.dump_acpi_tables(str(self.root / "ACPI"), str(src))
        self.assertIn("only root can read them", str(e.exception))
        self.assertFalse((self.root / "ACPI").exists())

    def test_a_kernel_without_acpi_says_so(self):
        self.assertIsNone(linuxhost.acpi_readable(str(self.root / "none")))
        with self.assertRaises(linuxhost.AcpiAccessError) as e:
            linuxhost.dump_acpi_tables(str(self.root / "ACPI"), str(self.root / "none"))
        self.assertIn("exposes no ACPI tables", str(e.exception))

    def test_the_scan_worker_dump_prints_the_reason_and_leaves_no_dsdt(self):
        with mock.patch.object(linuxhost, "dump_acpi_tables", side_effect=linuxhost.AcpiAccessError(linuxhost.ROOT_MESSAGE)), \
                mock.patch("sys.stderr") as err, contextlib.redirect_stdout(io.StringIO()):
            scan.linux_dump_acpi(str(self.root))
        self.assertIn("only root", "".join(c.args[0] for c in err.write.call_args_list))
        self.assertTrue(scan.check(str(self.root)))


LSBLK = {"blockdevices": [
    {"name": "zram0", "model": None, "tran": None, "rota": False, "size": 33283899392, "type": "disk", "mountpoints": ["[SWAP]"]},
    {"name": "loop0", "model": None, "tran": None, "rota": "0", "size": 1, "type": "loop", "mountpoints": [None]},
    {"name": "nvme0n1", "model": "SAMSUNG MZVL21T0HCLR-00BTW", "tran": "nvme", "rota": False, "size": 1024209543168,
     "type": "disk", "mountpoints": [None], "children": [
        {"name": "nvme0n1p1", "type": "part", "mountpoints": ["/boot/efi"]},
        {"name": "nvme0n1p2", "type": "part", "mountpoints": [None], "children": [
            {"name": "luks-root", "type": "crypt", "mountpoints": ["/home", "/"]}]}]},
    {"name": "sda", "model": "WDC  WD10EZEX-08WN4A0 ", "tran": "sata", "rota": "1", "size": "1000204886016", "type": "disk",
     "mountpoint": None},
    {"name": "sdb", "model": "SanDisk 3.2Gen1", "tran": "usb", "rota": True, "size": 30752000000, "type": "disk",
     "mountpoint": "/run/media/u/STICK"},
    {"name": "sr0", "model": "DVD RW", "tran": "sata", "rota": True, "size": 1, "type": "rom"},
]}


class StorageDrives(unittest.TestCase):
    def test_lsblk_rows_have_the_get_disk_shape(self):
        rows = {r["Number"]: r for r in linuxhost.lsblk_rows(LSBLK)}
        self.assertEqual(sorted(rows), ["nvme0n1", "sda", "sdb"])
        self.assertEqual(rows["nvme0n1"], {"Number": "nvme0n1", "Model": "SAMSUNG MZVL21T0HCLR-00BTW", "BusType": "NVMe",
                                           "MediaType": "SSD", "Size": 1024209543168, "IsBoot": True, "IsSystem": True})
        self.assertEqual((rows["sda"]["Model"], rows["sda"]["BusType"], rows["sda"]["MediaType"], rows["sda"]["Size"]),
                         ("WDC WD10EZEX-08WN4A0", "SATA", "HDD", 1000204886016))
        self.assertFalse(rows["sda"]["IsBoot"] or rows["sda"]["IsSystem"])
        self.assertEqual(rows["sdb"]["BusType"], "USB")

    def test_the_report_section_names_each_drive(self):
        section = scan._storage_drives_section(linuxhost.lsblk_rows(LSBLK))
        self.assertEqual(section["Disk nvme0n1"]["Model"], "SAMSUNG MZVL21T0HCLR-00BTW")
        self.assertTrue(section["Disk nvme0n1"]["System"])
        self.assertEqual(section["Disk sdb"]["Bus Type"], "USB")
        self.assertNotIn("System", section["Disk sda"])

    def test_an_old_lsblk_without_mountpoints_is_retried_and_failures_give_no_rows(self):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd[-1])
            if "MOUNTPOINTS" in cmd[-1]:
                return subprocess.CompletedProcess(cmd, 1, "", "unknown column")
            return subprocess.CompletedProcess(cmd, 0, json.dumps(LSBLK), "")
        self.assertEqual(len(linuxhost.storage_drive_rows(run)), 3)
        self.assertEqual(len(calls), 2)
        self.assertEqual(linuxhost.storage_drive_rows(lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("lsblk"))), [])

    def test_collect_storage_drives_reads_lsblk_on_linux_only(self):
        with mock.patch.object(scan.platform, "system", return_value="Linux"), \
                mock.patch.object(linuxhost, "storage_drive_rows", return_value=[{"Number": "sda", "Model": "X"}]) as rows:
            self.assertEqual(scan.collect_storage_drives(), [{"Number": "sda", "Model": "X"}])
        rows.assert_called_once()
        with mock.patch.object(scan.platform, "system", return_value="Darwin"), \
                mock.patch.object(linuxhost, "storage_drive_rows", side_effect=AssertionError("not on macOS")):
            self.assertEqual(scan.collect_storage_drives(), [])


class ScanController(Tmp):
    def capture(self):
        from p1401 import hwcapture  # noqa: PLC0415
        return hwcapture.empty_native("fixture")

    def test_the_worker_stops_before_the_sniffer_without_root(self):
        with mock.patch.object(scan.platform, "system", return_value="Linux"), \
                mock.patch.object(linuxhost, "acpi_readable", return_value=False), \
                mock.patch.object(scan.sys, "path", list(scan.sys.path)) as path, mock.patch("sys.stderr") as err:
            self.assertEqual(scan._sniffer_body(str(self.root), "a" * 32), scan.ACPI_UNREADABLE)
        self.assertNotIn(scan.SNIFFER, path)   # the Sniffer was never imported
        self.assertIn("only root", "".join(c.args[0] for c in err.write.call_args_list))

    def test_the_controller_records_why_and_returns_failure(self):
        from p1401 import hwcapture  # noqa: PLC0415
        out = self.root / "scan"
        with mock.patch.object(scan.platform, "system", return_value="Linux"), \
                mock.patch.object(hwcapture, "collect", return_value=self.capture()), \
                mock.patch.object(scan.subprocess, "run", return_value=subprocess.CompletedProcess([], scan.ACPI_UNREADABLE)), \
                mock.patch.object(linuxhost, "give_back") as back, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(scan._child(str(out)), 1)
        self.assertEqual(se._read(out / se.RECEIPT)["failure_type"], "acpi_tables_unreadable")
        back.assert_called_once_with(str(out))

    def test_the_wrapper_hands_back_even_when_the_scan_fails(self):
        with mock.patch.object(scan.platform, "system", return_value="Linux"), \
                mock.patch.object(scan, "_controller", side_effect=RuntimeError("boom")), \
                mock.patch.object(linuxhost, "give_back") as back:
            with self.assertRaises(RuntimeError):
                scan._child(str(self.root / "x"))
        back.assert_called_once()

    def test_windows_never_touches_the_linux_helpers(self):
        with mock.patch.object(scan.platform, "system", return_value="Windows"), \
                mock.patch.object(scan, "_controller", return_value=0), \
                mock.patch.object(linuxhost, "give_back", side_effect=AssertionError("linux only")):
            self.assertEqual(scan._child(str(self.root / "x")), 0)


class GiveBack(Tmp):
    def test_the_sudo_or_pkexec_user_gets_the_files(self):
        with mock.patch.object(linuxhost.os, "geteuid", return_value=0, create=True):
            self.assertEqual(linuxhost.invoking_user({"SUDO_UID": "1000", "SUDO_GID": "1001"}), (1000, 1001))
            self.assertIsNone(linuxhost.invoking_user({"SUDO_UID": "0"}))
            self.assertIsNone(linuxhost.invoking_user({}))
            with mock.patch.dict("sys.modules", {"pwd": mock.Mock(getpwuid=lambda uid: mock.Mock(pw_gid=77))}):
                self.assertEqual(linuxhost.invoking_user({"PKEXEC_UID": "1000"}), (1000, 77))
        with mock.patch.object(linuxhost.os, "geteuid", return_value=1000, create=True):
            self.assertIsNone(linuxhost.invoking_user({"SUDO_UID": "1000"}))

    def test_every_file_and_folder_is_chowned(self):
        (self.root / "ACPI").mkdir()
        (self.root / "ACPI" / "dsdt.aml").write_bytes(b"x")
        (self.root / "Report.json").write_text("{}")
        with mock.patch.object(linuxhost.os, "chown", create=True) as chown:
            self.assertTrue(linuxhost.give_back(str(self.root), (1000, 1000)))
        touched = {Path(c.args[0]).relative_to(self.root).as_posix() for c in chown.call_args_list}
        self.assertEqual(touched, {".", "ACPI", "ACPI/dsdt.aml", "Report.json"})
        self.assertFalse(linuxhost.give_back(str(self.root / "missing"), (1000, 1000)))


class BootTime(Tmp):
    def test_btime_from_proc_stat(self):
        stat = self.root / "stat"
        stat.write_text("cpu  1 2 3\nbtime 1794064309\nprocesses 5\n")
        self.assertEqual(linuxhost.boot_epoch(str(stat)), 1794064309.0)


class FrontEndCommands(unittest.TestCase):
    def run_cli(self, *args, env=None):
        root = Path(__file__).resolve().parents[1]
        return subprocess.run([sys.executable, "-E", "-s", "-B", "-m", "p1401.linuxhost", *args], cwd=root,
                              capture_output=True, text=True, timeout=60, env=env)

    def test_workdir_prints_the_xdg_folder(self):
        env = dict(os.environ, XDG_DATA_HOME="/data/u")
        p = self.run_cli("workdir", env=env)
        self.assertEqual((p.returncode, p.stdout.strip()), (0, "/data/u/NullMoth/1401"))

    def test_acpi_exit_code_says_whether_root_is_needed(self):
        p = self.run_cli("acpi")
        state = linuxhost.acpi_readable()
        self.assertEqual(p.returncode, {True: 0, False: 1, None: 2}[state])
        self.assertNotEqual(self.run_cli().returncode, 0)

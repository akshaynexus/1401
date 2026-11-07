"""The Linux window's reading of engine output and its decisions, without GTK (linux/one401_gtk, linux/1401-helper)."""
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "linux"))

from one401_gtk import engine, hwmap, loc, logic, reports, upload  # noqa: E402


def _helper():
    loader = importlib.machinery.SourceFileLoader("a1401_helper", str(ROOT / "linux" / "1401-helper"))
    spec = importlib.util.spec_from_loader("a1401_helper", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class EngineLines(unittest.TestCase):
    def test_progress_lines_move_the_bar_and_stay_out_of_the_log(self):
        self.assertEqual(logic.progress("PROGRESS 42 downloading macOS recovery from Apple"), 42)
        self.assertEqual(logic.progress("PROGRESS 140 x"), 100)
        self.assertEqual(logic.progress("PROGRESS -3 x"), 0)
        self.assertEqual(logic.progress("PROGRESS soon"), -1)
        self.assertIsNone(logic.progress("STEP erasing and writing disk 1"))

    def test_writer_markers(self):
        lines = ["STEP checking", "STOP Exception: 1401-OS-PARTITIONS: disk 1 holds APFS (macOS). Nothing was erased."]
        self.assertEqual(logic.os_partitions(lines), "disk 1 holds APFS (macOS). Nothing was erased.")
        self.assertIsNone(logic.os_partitions(["OK fine"]))
        self.assertTrue(logic.disk_renumbered(["STOP 1401-DISK-RENUMBERED: Windows gave the stick a new number"]))
        self.assertTrue(logic.scan_changed(["STOP The scan changed after the EFI was built. Build again."]))
        self.assertFalse(logic.scan_changed(["OK the stick is ready at /run/media/x/1401"]))

    def test_appcheck(self):
        self.assertEqual(logic.appcheck_newer(0, ["newest", "RESULT newer 2.8.0"]), "2.8.0")
        self.assertIsNone(logic.appcheck_newer(0, ["RESULT uptodate 2.7.2"]))
        self.assertIsNone(logic.appcheck_newer(0, ["RESULT unknown URLError"]))
        self.assertIsNone(logic.appcheck_newer(1, ["RESULT newer 2.8.0"]))
        self.assertEqual(logic.last_result(["RESULT a", "x", "RESULT ready 2.8.0 /tmp/x"]), "RESULT ready 2.8.0 /tmp/x")

    def test_efidoctor_and_diagnose_lines(self):
        out = ["checking /run/media/me/EFI", "  needs fixing: Lilu.kext after WhateverGreen", "  ok: SecureBootModel"]
        self.assertEqual(logic.doctor_lines(out), out[1:])
        self.assertTrue(logic.doctor_fixable(out))
        self.assertFalse(logic.doctor_fixable(["  cannot fix: no OC folder"]))
        self.assertEqual(logic.diagnose_lines(["Stopped at EB.MM.AKM", "  Turn off CSM"]), ["Turn off CSM"])


class BuildResult(unittest.TestCase):
    def _lines(self, obj, before=("Downloading OpenCore...", "iasl ok")):
        return list(before) + json.dumps(obj, indent=2).splitlines()

    def test_a_good_build_makes_the_summary_with_support_notices_only(self):
        obj = {"ok": True, "macos_version": "24.6.0", "smbios": "MacPro7,1",
               "notices": ["NullMoth NVIDIA driver: RTX 4070", "Graphics: AMD RX 6600 is native", "random note",
                           "Laptop display note: check the panel", "Intel I225-LM vPro note"]}
        out = logic.read_build(0, self._lines(obj))
        self.assertTrue(out.ok)
        self.assertEqual((out.darwin, out.macos_full), ("24", "24.6.0"))
        self.assertEqual(out.summary.split("\n"), [
            "macOS Sequoia for this PC, as a MacPro7,1. The startup files passed OpenCore's own check.",
            "NullMoth NVIDIA driver: RTX 4070", "Graphics: AMD RX 6600 is native", "Laptop display note: check the panel",
            "Intel I225-LM vPro note"])

    def test_a_refused_build_names_the_error_or_the_exit_code(self):
        out = logic.read_build(1, self._lines({"ok": False, "error": "no supported GPU"}))
        self.assertFalse(out.ok)
        self.assertEqual(out.message, "The build stopped: no supported GPU")
        out = logic.read_build(3, self._lines({"ok": False, "error": ""}))
        self.assertEqual(out.message, "The build stopped: Engine exit code 3.")
        # exit code wins even when the JSON says ok
        self.assertFalse(logic.read_build(1, self._lines({"ok": True, "macos_version": "25.0", "smbios": "x"})).ok)

    def test_output_without_a_result_cannot_be_read(self):
        with self.assertRaises(ValueError):
            logic.read_build(1, ["Traceback (most recent call last):", "boom"])
        with self.assertRaises(ValueError):
            logic.read_build(0, ["{ not json"])

    def test_tahoe_name(self):
        out = logic.read_build(0, self._lines({"ok": True, "macos_version": "25.1.0", "smbios": "iMacPro1,1"}))
        self.assertTrue(out.summary.startswith("macOS Tahoe for this PC, as a iMacPro1,1."))


class EngineCalls(unittest.TestCase):
    def test_build_and_guide_and_scan_arguments_match_the_windows_app(self):
        self.assertEqual(logic.build_args("/w/scan/Report.json", "/w/scan/ACPI", "/w/efi", False, False, [], ""),
                         ["p1401", "build", "/w/scan/Report.json", "/w/scan/ACPI", "/w/efi", "--json", "--macos", "24"])
        self.assertEqual(logic.build_args("r", "a", "e", True, True, ["/m/opencore-1.txt", "/m/panic-1.txt"], "PCI configuration begin"),
                         ["p1401", "build", "r", "a", "e", "--json", "--macos", "25", "--experimental-drivers",
                          "--boot-log", "/m/opencore-1.txt", "--boot-log", "/m/panic-1.txt", "--stopped-at", "PCI configuration begin"])
        self.assertEqual(logic.guide_args("r.json", "/w/efi", "24.6.0", "/w/BIOS-steps.html"),
                         ["p1401.guide", "r.json", "/w/efi/EFI/OC/config.plist", "--macos", "24.6.0", "--html", "/w/BIOS-steps.html"])
        self.assertEqual(logic.firmware_guide_args("r.json", "a" * 32, "/w/f.html"),
                         ["p1401.guide", "--bios-only", "r.json", "--run-id", "a" * 32, "--html", "/w/f.html"])
        self.assertEqual(logic.scan_args("/w/scan-x", "x"), ["p1401.scan", "/w/scan-x", "--run-id", "x"])

    def test_write_arguments(self):
        d = logic.UsbDisk({"Number": 2, "FriendlyName": "SanDisk 3.2Gen1", "UniqueId": "usb-SanDisk-0:0", "BusType": "USB",
                           "Size": 61530439680})
        self.assertEqual(logic.write_args(d, "/w/efi", "24"),
                         ["p1401.usbwriter", "write", "2", "/w/efi", "24", "--stick-id", "usb-SanDisk-0:0",
                          "--stick-size", "61530439680", "--stick-name", "SanDisk 3.2Gen1"])
        big = logic.UsbDisk({"Number": 3, "FriendlyName": "Big", "UniqueId": "u", "BusType": "USB", "Size": 300 * logic.GiB})
        args = logic.write_args(big, "/e", 25, driver="/n/nullmoth-nvidia-1.0.0.tar.gz", extra="/n/1401-Mac-2.7.2.zip",
                                profile="/w/Report.json", arm_apps=True)
        self.assertEqual(args[11:], ["--driver", "/n/nullmoth-nvidia-1.0.0.tar.gz", "--extra", "/n/1401-Mac-2.7.2.zip",
                                     "--profile", "/w/Report.json", "--allow-large", "--arm-apps"])

    def test_engine_command_isolates_python_and_routes_root_steps_through_pkexec(self):
        cmd = engine.command(["p1401", "build", "r", "a", "e"])
        self.assertEqual(cmd[1:8], ["-E", "-s", "-B", "-X", "utf8", "-u", "-m"])
        self.assertEqual(cmd[8:], ["p1401", "build", "r", "a", "e"])
        with mock.patch.object(engine, "pkexec_path", return_value="/usr/bin/pkexec"):
            priv = engine.command(["p1401.scan", "/w/scan-x", "--run-id", "x"], privileged=True)
        self.assertEqual(priv[:2], ["/usr/bin/pkexec", engine.HELPER])
        self.assertEqual(priv[-4:], ["p1401.scan", "/w/scan-x", "--run-id", "x"])
        env = engine.environment()
        for name in engine.STRIPPED:
            self.assertNotIn(name, env)
        self.assertEqual(env["PYTHONNOUSERSITE"], "1")
        self.assertIn(env["A1401_LANG"], ("en", "zh-CN"))
        self.assertEqual([engine.stage(a) for a in (["p1401.scan", "x"], ["p1401", "build"], ["p1401.usbwriter", "write"],
                                                     ["p1401.guide"])], ["scan", "build", "write", "operation"])

    def test_run_records_reports_and_spots_a_dismissed_password_prompt(self):
        class FakeProcess:
            def __init__(self, lines, rc):
                self.stdout, self.rc = io.StringIO("".join(l + "\n" for l in lines)), rc

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def wait(self):
                return self.rc

        with tempfile.TemporaryDirectory() as work, mock.patch.object(engine, "pkexec_path", return_value="/usr/bin/pkexec"):
            seen = []
            res = engine.run_blocking(["p1401.scan", "/w", "--run-id", "x"], seen.append, privileged=True, ver="9.9.9",
                                      work=work, popen=lambda *a, **k: FakeProcess(["Error executing command as another user: Not authorized"], 126))
            self.assertTrue(res.refused)
            self.assertEqual(res.rc, 126)
            self.assertTrue(res.report.saved)
            self.assertIn("administrator password was not given", seen[-1])
            res = engine.run_blocking(["p1401.scan", "/w", "--run-id", "x"], seen.append, privileged=True, ver="9.9.9",
                                      work=work, popen=lambda *a, **k: FakeProcess([engine.HELPER_MARK + "; running p1401.scan as root", "Scan run x"], 1))
            self.assertFalse(res.refused)
            with open(res.report.path, encoding="utf-8") as fh:
                saved = fh.read()
            self.assertIn("exit code: 1", saved)
            self.assertIn("stage: scan", saved)
            self.assertEqual(len([n for n in os.listdir(os.path.join(work, "diagnostic-reports")) if "-scan-" in n]), 4)


class Sticks(unittest.TestCase):
    RAW = [
        {"Number": 0, "FriendlyName": "Samsung SSD 980", "UniqueId": "nvme-1", "BusType": "NVMe", "Size": 10 ** 12, "IsSystem": True},
        {"Number": 3, "FriendlyName": "Kingston", "UniqueId": "k", "BusType": "USB", "Size": 16008609792},
        {"Number": 1, "FriendlyName": "SanDisk 3.2Gen1", "UniqueId": "s", "BusType": "USB", "Size": 61530439680, "Path": "/dev/sda"},
        {"Number": 2, "FriendlyName": "Tiny", "UniqueId": "t", "BusType": "USB", "Size": 1 << 30},
        {"Number": 4, "FriendlyName": "Boot stick", "UniqueId": "b", "BusType": "USB", "Size": 8 << 30, "IsBoot": True},
        {"Number": 5, "FriendlyName": "Running 1401", "UniqueId": "r", "BusType": 7, "Size": 8 << 30, "Path": "/dev/sdc"},
        {"Number": 6, "FriendlyName": "Locked", "UniqueId": "l", "BusType": "USB", "Size": 8 << 30, "Eligible": False,
         "Reasons": ["Linux cannot write to this stick (it is read-only)"]},
        {"Number": 7, "FriendlyName": "Backup", "UniqueId": "bk", "BusType": "USB", "Size": 1 << 40, "Eligible": False,
         "Reasons": ["1024 GiB is larger than any USB stick 1401 expects - confirm it is not a backup drive"]},
    ]

    def test_the_writer_list_is_read_after_its_notes(self):
        out = "OK Get-Disk listed nothing; 2 disk(s) read from MSFT_Disk instead\n" + json.dumps(self.RAW, indent=2)
        self.assertEqual([d["Number"] for d in logic.parse_disk_list(out)], [0, 3, 1, 2, 4, 5, 6, 7])
        self.assertEqual(logic.parse_disk_list(json.dumps(self.RAW[1]))[0]["Number"], 3)
        with self.assertRaises(ValueError):
            logic.parse_disk_list("STOP no disks")

    def test_only_usable_usb_sticks_are_offered(self):
        offered, skipped = logic.offered_disks(self.RAW, {"/dev/sdc"}, {"/dev/sda": ["/run/media/me/1401"]})
        self.assertEqual([d.number for d in offered], [1, 3, 7])
        self.assertEqual(str(offered[0]), "Disk 1:  SanDisk 3.2Gen1  -  61.5 GB   (/dev/sda /run/media/me/1401)")
        self.assertEqual(str(offered[1]), "Disk 3:  Kingston  -  16 GB")
        self.assertEqual(offered[0].mounts, ["/run/media/me/1401"])
        text = "\n".join(skipped)
        self.assertIn("Disk 0 Samsung SSD 980 (1000 GB): connected by NVMe, not USB", text)
        self.assertIn("too small, 1401 needs 2 GB", text)
        self.assertIn("Boot stick (8.6 GB): Linux runs from it", text)
        self.assertIn("Running 1401  -  8.6 GB   (/dev/sdc): 1401 is running from it", text)
        self.assertIn("Disk 6:  Locked  -  8.6 GB: Linux cannot write to this stick (it is read-only)", text)

    def test_boot_logs_on_a_stick(self):
        with tempfile.TemporaryDirectory() as stick:
            sent = os.path.join(stick, "NullMoth", "sent-logs")
            os.makedirs(sent)
            for name in ("opencore-2026-11-01-101010.txt", "panic-1.txt", "panic-3.txt", "notes.txt"):
                pathlib.Path(stick, name).write_text("x")
            for name in ("opencore-2026-11-05-090000.txt", "panic-2.txt"):
                pathlib.Path(sent, name).write_text("x")
            got = [os.path.relpath(f, stick) for f in logic.stick_boot_logs([stick, stick])]
            self.assertEqual(got, ["NullMoth/sent-logs/opencore-2026-11-05-090000.txt", "panic-3.txt", "NullMoth/sent-logs/panic-2.txt"])
            self.assertEqual(logic.stick_boot_logs(["/nonexistent-1401"]), [])

    def test_config_redaction_and_provenance(self):
        xml = ("<key>SystemSerialNumber</key>\n<string>C02XYZ</string><key>ROM</key><data>ESIzRFVm</data>"
               "<key>Other</key><string>kept</string>")
        red = logic.redact_config(xml)
        self.assertIn("<string>REMOVED</string>", red)
        self.assertIn("<data>AAAAAAAA</data>", red)
        self.assertIn("<string>kept</string>", red)
        with tempfile.TemporaryDirectory() as stick:
            cfg = pathlib.Path(stick, "config.plist")
            cfg.write_bytes(b"<plist/>")
            self.assertEqual(logic.stick_provenance(stick, str(cfg)), "this stick's OpenCore EFI was not built by 1401")
            os.makedirs(os.path.join(stick, "NullMoth"))
            profile = pathlib.Path(stick, "NullMoth", "system-profile.json")
            profile.write_text(json.dumps({"efi": {"OC/config.plist": hashlib.sha256(b"<plist/>").hexdigest()}}))
            self.assertEqual(logic.stick_provenance(stick, str(cfg)), "")
            cfg.write_bytes(b"<plist>edited</plist>")
            self.assertEqual(logic.stick_provenance(stick, str(cfg)), "this stick's OpenCore config was changed after 1401 built it")

    def test_companion_prefers_this_version_then_the_highest(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            for d, n in ((a, "nullmoth-nvidia-1.9.0.tar.gz"), (b, "nullmoth-nvidia-1.10.0.tar.gz"), (a, "nullmoth-nvidia-2.7.2.tar.gz")):
                pathlib.Path(d, n).write_text("x")
            self.assertTrue(logic.companion([a, b], "nullmoth-nvidia-", ".tar.gz", "2.7.2").endswith("2.7.2.tar.gz"))
            self.assertTrue(logic.companion([a, b], "nullmoth-nvidia-", ".tar.gz", "3.0.0").endswith("2.7.2.tar.gz"))
            os.remove(os.path.join(a, "nullmoth-nvidia-2.7.2.tar.gz"))
            self.assertTrue(logic.companion([a, b], "nullmoth-nvidia-", ".tar.gz", "3.0.0").endswith("1.10.0.tar.gz"))
            self.assertIsNone(logic.companion([a, "/nonexistent-1401"], "1401-Mac-", ".zip", "2.7.2"))

    def test_facts_from_the_report(self):
        rows = logic.facts({"Motherboard": {"Name": "B550 AORUS"}, "CPU": {"Manufacturer": "AMD"},
                            "GPU": {"NVIDIA GeForce RTX 4070": {}}, "Network": {"Intel I225-V": {}, "MT7922": {}}})
        self.assertEqual(rows, [("Board", "B550 AORUS"), ("Processor", "AMD"), ("Graphics", "NVIDIA GeForce RTX 4070"),
                                ("Network", "Intel I225-V"), ("Network", "MT7922")])


class Reports(unittest.TestCase):
    def test_identity_is_removed(self):
        text = "user: akshay on host predator, /home/akshay/.local/share/NullMoth/1401 and akshay's GPU Intel(R) UHD"
        clean = reports.clean_identity(text, "akshay", "predator", "/home/akshay")
        self.assertNotIn("akshay", clean)
        self.assertNotIn("predator", clean)
        self.assertIn("$HOME/.local/share/NullMoth/1401", clean)
        # a user named like a vendor keeps the vendor where it names hardware
        self.assertIn("Intel(R) UHD", reports.clean_identity("login intel; GPU Intel(R) UHD", "intel", "box", "/home/intel"))
        self.assertIn("login user", reports.clean_identity("login intel; GPU Intel(R) UHD", "intel", "box", "/home/intel"))

    def test_reports_are_saved_found_and_local(self):
        with tempfile.TemporaryDirectory() as work:
            out = reports.save(work, "build-final", "2.7.2", "hello")
            self.assertTrue(out.saved)
            self.assertEqual(reports.find(work), [out.path])
            self.assertTrue(reports.is_local(work, out.path))
            self.assertFalse(reports.is_local(work, os.path.join(work, "x.txt")))
            self.assertFalse(reports.save(work, "Bad Kind", "2.7.2", "x").saved)
            with open(out.path, encoding="utf-8") as fh:
                self.assertTrue(fh.read().startswith("1401 2.7.2 local build-final report\n"))

    def test_scan_evidence_selection(self):
        with tempfile.TemporaryDirectory() as work:
            token = "0123456789abcdef0123456789abcdef"
            d = reports.run_directory(work, token)
            os.makedirs(d)
            pathlib.Path(d, "scan-evidence.json").write_text(json.dumps({"schema": "nullmoth-scan-evidence/1", "run_id": token}))
            reports.remember(work, token)
            current = reports.find_evidence(work, token, False)
            saved = reports.find_evidence(work, None, True)
            self.assertTrue(current.current_run)
            self.assertFalse(saved.current_run)
            self.assertEqual(current.path, saved.path)
            self.assertIsNone(reports.find_evidence(work, None, False))
            self.assertIsNone(reports.find_evidence(work, "f" * 32, True))
            with self.assertRaises(ValueError):
                reports.run_directory(work, "../x")

    def test_a_scan_from_before_this_boot_is_history(self):
        with tempfile.TemporaryDirectory() as d:
            report = pathlib.Path(d, "Report.json")
            report.write_text("{}")
            os.utime(report, (1000, 1000))
            self.assertTrue(reports.older_than_this_boot(d, booted=2000))
            self.assertFalse(reports.older_than_this_boot(d, booted=1030))
            self.assertFalse(reports.older_than_this_boot(None))


class HardwareMap(unittest.TestCase):
    def test_codec_hid_and_acpi_parsing(self):
        self.assertEqual(hwmap.codec("Codec: Realtek ALC256\nVendor Id: 0x10ec0256\nNode 0x01 [Audio Function Group] wake"),
                         {"audio_codec_vendor_device": 0x10ec0256, "audio_codec_function_group": 1})
        self.assertEqual(hwmap.hid_usage(bytes([0x05, 0x01, 0x09, 0x02, 0xA1, 0x01, 0x09, 0x01])), (1, 2))
        self.assertEqual(hwmap.hid_usage(bytes([0x06, 0x0C, 0x00, 0x09, 0x01, 0xA1, 0x01])), (0x0C, 1))
        self.assertEqual(hwmap.acpi_ids(["PNP0A08", "acpi", "INT33D5", "ACPI0003", "lower-x"]), ["ACPI0003", "INT33D5", "PNP0A08"])
        self.assertEqual(hwmap.line({"pci_vendor": 4318, "pci_device": 9989}, ["PNP0A08"]),
                         "device pci_device=9989 pci_vendor=4318 acpi_ids=PNP0A08")

    def test_collect_from_a_fake_sysfs(self):
        with tempfile.TemporaryDirectory() as root:
            sys_root, proc_root = os.path.join(root, "sys"), os.path.join(root, "proc")
            pci = os.path.join(sys_root, "bus/pci/devices/0000:01:00.0")
            os.makedirs(pci)
            for name, value in (("vendor", "0x10de"), ("device", "0x2786"), ("subsystem_vendor", "0x1462"),
                                ("subsystem_device", "0x5144"), ("revision", "0xa1"), ("class", "0x030000")):
                pathlib.Path(pci, name).write_text(value + "\n")
            usb = os.path.join(sys_root, "bus/usb/devices/1-1")
            os.makedirs(usb)
            for name, value in (("idVendor", "0781"), ("idProduct", "5581"), ("bDeviceClass", "00"), ("serial", "SECRET123")):
                pathlib.Path(usb, name).write_text(value + "\n")
            os.makedirs(os.path.join(sys_root, "bus/acpi/devices/PNP0C0A:00"))
            pathlib.Path(sys_root, "bus/acpi/devices/PNP0C0A:00/modalias").write_text("acpi:PNP0C0A:\n")
            os.makedirs(os.path.join(sys_root, "bus/acpi/devices/LNXSYSTM:00"))
            os.makedirs(os.path.join(proc_root, "asound/card0"))
            pathlib.Path(proc_root, "asound/card0/codec#0").write_text("Vendor Id: 0x10ec0897\n")
            text = hwmap.collect(sys_root, proc_root)
        self.assertNotIn("SECRET", text)
        lines = text.splitlines()
        self.assertEqual(lines[0], "hardware map 1")
        self.assertIn("device pci_class=196608 pci_device=10118 pci_revision=161 pci_subsystem_device=20804 "
                      "pci_subsystem_vendor=5218 pci_vendor=4318", lines)
        self.assertIn("device usb_class=0 usb_product=21889 usb_vendor=1921", lines)
        self.assertIn("device audio_codec_vendor_device=283904151", lines)
        self.assertIn("device acpi_ids=PNP0C0A", lines)
        self.assertEqual(lines[-2:], ["devices_listed: 4", "devices_without_identifiers: 1"])


class Translations(unittest.TestCase):
    def test_the_table_is_loc_cs(self):
        cs = ROOT / "windows" / "App" / "Loc.cs"
        if not cs.exists():
            self.skipTest("windows/App/Loc.cs is not in this checkout")
        with open(loc.TABLE, encoding="utf-8") as fh:
            table = json.load(fh)
        self.assertEqual(table, loc.parse_loc_cs(str(cs)),
                         "linux/one401_gtk/loc_zh.json is out of date: run python3 -m one401_gtk.loc --regenerate in linux/")

    def test_language_and_fallback(self):
        self.assertEqual(loc._detect({"A1401_LANG": "zh-CN", "LANG": "en_US.UTF-8"}), "zh")
        self.assertEqual(loc._detect({"LANG": "zh_CN.UTF-8"}), "zh")
        self.assertEqual(loc._detect({"LC_ALL": "de_DE.UTF-8", "LANG": "zh_CN.UTF-8"}), "en")
        with mock.patch.object(loc, "LANGUAGE", "zh"):
            self.assertEqual(loc.T("Check this PC"), "检测本机")
            self.assertEqual(loc.T("An untranslated line"), "An untranslated line")
            self.assertEqual(loc.F("Found {0} USB stick(s).", 2), "找到 2 个 U 盘。")
        self.assertEqual(loc.T("Done."), "Done.")
        self.assertNotIn("\r", loc.T("The NVIDIA driver is up to date.\r\n\r\n"))


class Helper(unittest.TestCase):
    def setUp(self):
        self.h = _helper()
        self.uid = os.getuid()

    def _refused(self, fn, *args):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out, self.assertRaises(SystemExit) as stop:
            fn(*args)
        self.assertEqual(stop.exception.code, 2)
        self.assertIn("STOP 1401-HELPER:", out.getvalue())

    def test_scan_arguments(self):
        with tempfile.TemporaryDirectory() as work:
            work = os.path.realpath(work)
            token = "a" * 32
            scan = os.path.join(work, "scan-" + token)
            os.makedirs(scan)
            self.assertEqual(self.h.check_scan([scan, "--run-id", token], self.uid), [scan])
            self._refused(self.h.check_scan, [scan, "--run-id", "b" * 32], self.uid)
            self._refused(self.h.check_scan, [scan, "--run-id", "../x"], self.uid)
            self._refused(self.h.check_scan, [scan], self.uid)
            self._refused(self.h.check_scan, [scan, "--run-id", token], self.uid + 1)
            link = os.path.join(work, "link")
            os.symlink(work, link)
            self._refused(self.h.check_scan, [os.path.join(link, "scan-" + token), "--run-id", token], self.uid)

    def test_write_arguments(self):
        with tempfile.TemporaryDirectory() as work:
            work = os.path.realpath(work)
            efi = os.path.join(work, "efi")
            os.makedirs(efi)
            report = pathlib.Path(work, "Report.json")
            report.write_text("{}")
            ok = ["write", "1", efi, "24", "--stick-id", "usb-x", "--stick-size", "123", "--stick-name", "Stick",
                  "--profile", str(report), "--allow-large", "--arm-apps", "--erase-os-partitions"]
            self.assertEqual(self.h.check_write(ok, self.uid), [efi])
            self._refused(self.h.check_write, ["list"], self.uid)
            self._refused(self.h.check_write, ["write", "1; rm", efi, "24"], self.uid)
            self._refused(self.h.check_write, ["write", "1", efi, "24", "--recovery-dir", "/"], self.uid)
            self._refused(self.h.check_write, ["write", "1", efi, "24", "--driver", "/nonexistent-1401.tar.gz"], self.uid)
            self._refused(self.h.check_write, ["write", "1", efi, "24", "--stick-size", "big"], self.uid)
            self._refused(self.h.check_write, ["write", "1", "relative/efi", "24"], self.uid)

    def test_only_root_and_only_two_steps(self):
        if os.geteuid() != 0:
            self._refused(self.h.main, ["p1401.scan", "/x", "--run-id", "a" * 32])
        with mock.patch.object(self.h.os, "geteuid", return_value=0), \
                mock.patch.dict(os.environ, {"PKEXEC_UID": str(self.uid)}):
            self._refused(self.h.main, ["p1401", "build", "a", "b", "c"])
            self._refused(self.h.main, ["--lang", "zh-CN", "os", "x"])
        with mock.patch.object(self.h.os, "geteuid", return_value=0), mock.patch.dict(os.environ, {}, clear=True):
            self._refused(self.h.main, ["p1401.scan"])


class Upload(unittest.TestCase):
    def test_request_shape(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d, "opencore-1.txt")
            path.write_bytes(b"log\0\0")
            req, sha = upload.request(str(path), "opencore-1.txt", "startup log from the stick", "2.7.2", "1401-app-00")
            self.assertEqual(req.data, b"log")
            self.assertEqual(sha, hashlib.sha256(b"log").hexdigest())
            self.assertEqual(req.get_header("X-content-sha256"), sha)
            meta = req.get_header("X-meta")
            meta = json.loads(__import__("base64").urlsafe_b64decode(meta + "=" * (-len(meta) % 4)))
            self.assertEqual(meta["batch"], "1401-app-00")
            self.assertTrue(meta["consent"])
        self.assertEqual(upload.send_name("/w/scan-x/scan-evidence.json"), "scan-evidence.json.txt")
        self.assertTrue(upload.new_batch().startswith("1401-app-"))


if __name__ == "__main__":
    unittest.main()

"""Build the Mac setup and Your BIOS steps on a Linux host: the download cache in the XDG data folder, the boot-time check
only for this machine's own scan, iasl from the system when the engine folder has none, and BIOS steps that name the
Linux way to restart. Windows wording and paths are checked to stay as they were."""
import contextlib
import io
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from p1401 import engine, guide, linuxhost, loc


class OckCache(unittest.TestCase):
    def test_linux_uses_the_xdg_data_folder(self):
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": "/data/u"}, clear=False), mock.patch.object(engine.sys, "platform", "linux"):
            os.environ.pop("LOCALAPPDATA", None)
            self.assertEqual(engine._ock_cache(), "/data/u/NullMoth/1401/OCK_Files")

    def test_windows_keeps_localappdata(self):
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": r"C:\Users\u\AppData\Local"}), mock.patch.object(engine.sys, "platform", "win32"):
            self.assertEqual(engine._ock_cache(), os.path.join(r"C:\Users\u\AppData\Local", "NullMoth", "1401", "OCK_Files"))

    def test_other_systems_keep_the_engine_folder(self):
        with mock.patch.dict(os.environ, {}, clear=False), mock.patch.object(engine.sys, "platform", "darwin"):
            os.environ.pop("LOCALAPPDATA", None)
            self.assertEqual(engine._ock_cache(), os.path.join(engine.REPO, "upstream", "OpCore-Simplify", "OCK_Files"))


class StaleScan(unittest.TestCase):
    def report(self, evidence):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        path = os.path.join(d, "Report.json")
        Path(path).write_text("{}")
        if evidence:
            Path(d, "scan-evidence.json").write_text("{}")
        os.utime(path, (1000, 1000))
        return path

    def test_this_machines_own_old_scan_is_refused_on_linux(self):
        with mock.patch.object(engine.sys, "platform", "linux"), \
                mock.patch.object(linuxhost, "boot_epoch", return_value=time.time() - 60):
            message = engine.stale_scan_message(self.report(evidence=True))
        self.assertIn("checked before its last restart", message)
        self.assertIn("Linux started", message)

    def test_a_copied_in_report_is_not_a_scan_of_this_boot(self):
        with mock.patch.object(engine.sys, "platform", "linux"), \
                mock.patch.object(linuxhost, "boot_epoch", side_effect=AssertionError("not asked")):
            self.assertIsNone(engine.stale_scan_message(self.report(evidence=False)))

    def test_windows_wording_is_unchanged(self):
        with mock.patch.object(engine.sys, "platform", "win32"):
            message = engine.stale_scan_message(self.report(evidence=False), booted=time.time() - 60)
        self.assertIn(", Windows started ", message)

    def test_linux_boot_epoch_comes_from_linuxhost(self):
        with mock.patch.object(engine.sys, "platform", "linux"), mock.patch.object(linuxhost, "boot_epoch", return_value=123.0):
            self.assertEqual(engine.boot_epoch(), 123.0)


class NetworkHelp(unittest.TestCase):
    def test_the_proxy_advice_fits_the_system(self):
        with mock.patch.object(engine.sys, "platform", "linux"):
            self.assertIn("https_proxy", engine.network_help("https://github.com/x"))
        with mock.patch.object(engine.sys, "platform", "win32"):
            self.assertIn("a proxy set in Windows (Settings > Network & internet > Proxy - 1401 uses it), then build again.",
                          engine.network_help("https://github.com/x"))


class Iasl(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.calls = []
        test = self

        class DSDT:
            bundled = None

            def check_iasl(self, legacy=False, try_downloading=True):
                test.calls.append(try_downloading)
                return DSDT.bundled if not try_downloading else "/downloaded/iasl"
        self.DSDT = DSDT
        scripts = types.ModuleType("Scripts")
        scripts.__path__ = []
        scripts.dsdt = types.SimpleNamespace(DSDT=DSDT)
        patcher = mock.patch.dict(sys.modules, {"Scripts": scripts, "Scripts.dsdt": scripts.dsdt})
        patcher.start()
        self.addCleanup(patcher.stop)
        engine._linux_iasl()

    def exe(self, name):
        p = os.path.join(self.tmp.name, name)
        Path(p).write_text("#!/bin/sh\n")
        os.chmod(p, 0o755)
        return p

    def test_the_engine_folder_copy_comes_first(self):
        self.DSDT.bundled = "/engine/Scripts/iasl"
        with mock.patch("shutil.which", return_value=self.exe("iasl")), mock.patch.dict(os.environ, {"A1401_IASL": ""}):
            self.assertEqual(self.DSDT().check_iasl(), "/engine/Scripts/iasl")
        self.assertEqual(self.calls, [False])

    def test_the_system_iasl_is_used_before_any_download(self):
        system = self.exe("iasl")
        with mock.patch("shutil.which", return_value=system), mock.patch.dict(os.environ, {"A1401_IASL": ""}):
            self.assertEqual(self.DSDT().check_iasl(), system)
        self.assertEqual(self.calls, [False])

    def test_an_explicit_choice_wins_and_a_download_is_the_last_resort(self):
        chosen = self.exe("my-iasl")
        self.DSDT.bundled = "/engine/Scripts/iasl"
        with mock.patch.dict(os.environ, {"A1401_IASL": chosen}):
            self.assertEqual(self.DSDT().check_iasl(), chosen)
        self.DSDT.bundled = None
        with mock.patch("shutil.which", return_value=None), mock.patch.dict(os.environ, {"A1401_IASL": ""}), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self.DSDT().check_iasl(), "/downloaded/iasl")
        self.assertIn("acpica", out.getvalue())

    def test_patching_twice_does_not_stack(self):
        first = self.DSDT.check_iasl
        engine._linux_iasl()
        self.assertIs(self.DSDT.check_iasl, first)


REPORT = {"Motherboard": {"Name": "ASUSTEK ROG STRIX G733ZW", "Chipset": "Alder Lake", "Platform": "Laptop"},
          "BIOS": {"Firmware Type": "UEFI", "Secure Boot": "Disabled"},
          "CPU": {"Manufacturer": "Intel", "Processor Name": "12th Gen Intel(R) Core(TM) i9-12900H", "Codename": "Alder Lake-H"},
          "GPU": {}}


class BiosSteps(unittest.TestCase):
    def text(self, platform):
        with mock.patch.object(sys, "platform", platform):
            return guide.render_text(guide.build_guide(REPORT, {}, macos="macOS 15"))

    def test_linux_names_its_own_restart_commands(self):
        text = self.text("linux")
        self.assertIn("systemctl reboot --firmware-setup", text)
        self.assertIn("efibootmgr --bootnext", text)
        self.assertIn("Let Linux start once", text)
        self.assertIn("If this PC also has Windows with BitLocker on", text)
        self.assertNotIn("in the app. Windows restarts", text)

    def test_windows_steps_are_unchanged(self):
        text = self.text("win32")
        self.assertIn("Click **Restart into BIOS setup** in the app.", text)
        self.assertIn("Restart now and pick the USB stick (Use a device)", text)
        self.assertIn("Then the app pauses BitLocker", text)
        self.assertNotIn("systemctl", text)

    def test_the_linux_lines_are_translated(self):
        for line in ("Restart straight into your BIOS setup: in a terminal, run `systemctl reboot --firmware-setup`.",
                     "Change the settings below, then Save & Exit (on most boards [[F10]]). Let Linux start once to check it still boots."):
            self.assertIn(line, loc.ZH)
        self.assertTrue(any("efibootmgr --bootnext" in key for key in loc.ZH))
        self.assertTrue(any(key.startswith("If this PC also has Windows with BitLocker on") for key in loc.ZH))

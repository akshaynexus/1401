"""Acer Predator Helios 300 G3-571 machine profile (p1401/helios300.py) against a release manifest made the way
macos_helios300's tools/release/make_manifest.py makes one (zipped bundles, per-file SHA-256, OpenCore entries).

The hardware report is tests/fixtures/helios300/Report.json: 1401's own Linux scan of the owner's laptop (2026-11-08,
nvidia-macos-driver research/hardware/helios300/raw/1401-dump), with the per-scan disk inventory removed. The ACPI
tables are Acer firmware and are not in this repository; test_full_build runs the real engine build + ocvalidate
when A1401_HELIOS_ACPI points at a dump of them."""
import copy
import hashlib
import io
import json
import os
import plistlib
import re
import tempfile
import unittest
import zipfile
from unittest import mock

from p1401 import engine, helios300, nullmoth, policy

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT = os.path.join(HERE, "fixtures", "helios300", "Report.json")
BOOT = helios300.BOOT
URL = "https://github.com/akshaynexus/macos_helios300/releases/download/v0.1.0/"


def report():
    with open(REPORT, encoding="utf-8") as fh:
        return json.load(fh)


def _zip_bundle(name, files):
    """files: {relative path: bytes} -> (zip bytes, contents_sha256), entries under <name>/ as make_manifest writes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name + "/", b"")
        for rel, data in sorted(files.items()):
            info = zipfile.ZipInfo(f"{name}/{rel}", (1980, 1, 1, 0, 0, 0))
            info.external_attr = (0o100755 if rel.startswith("Contents/MacOS/") else 0o100644) << 16
            z.writestr(info, data)
    return buf.getvalue(), {rel: hashlib.sha256(d).hexdigest() for rel, d in files.items()}


def _kext(name, exe, order, requires=(), hardware=(), minimum="25.0.0", maximum="", conflicts=()):
    data, contents = _zip_bundle(name, {"Contents/Info.plist": plistlib.dumps({"CFBundleIdentifier": "x." + exe}),
                                        f"Contents/MacOS/{exe}": b"\xcf\xfa\xed\xfe" + exe.encode()})
    art = {"name": name, "kind": "kext", "bundle_id": "x." + exe, "version": "0.1.0", "file": name + ".zip",
           "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "url": URL + name + ".zip",
           "contents_sha256": contents, "licence": "GPL-2.0-only", "status": "test", "install": "opencore",
           "opencore": {"order": order, "BundlePath": name, "ExecutablePath": f"Contents/MacOS/{exe}", "PlistPath": "Contents/Info.plist",
                        "Arch": "x86_64", "MinKernel": minimum, "MaxKernel": maximum, "Enabled": True, "Comment": f"{exe} - test"},
           "requires": list(requires), "hardware": list(hardware), "source": {"tree": "self", "path": "x"}, "built_by": "cross"}
    if conflicts:
        art["conflicts"] = list(conflicts)
    return art, data


def _file(name, kind, install, data, **extra):
    art = {"name": name, "kind": kind, "version": "0.1.0", "file": name, "sha256": hashlib.sha256(data).hexdigest(),
           "size": len(data), "url": URL + name, "licence": "GPL-2.0-only", "status": "test", "install": install,
           "requires": [], "source": {"tree": "self", "path": "x"}, "built_by": "iasl"}
    art.update(extra)
    return art, data


GPU = {"bus": "pci", "vendor": "10de", "device": "1c20", "subsystem_vendor": "1025", "subsystem_device": "118b"}
WIFI = {"bus": "pci", "vendor": "168c", "device": "003e", "subsystem_vendor": "11ad", "subsystem_device": "0807"}
BTH = {"bus": "usb", "vendor": "04ca", "device": "3016"}


POST_011 = [
    {"step": "gpu", "order": 10, "what": "PG106FB and PG106Accel into /Library/Extensions",
     "artifacts": ["install-gpu.sh", "PG106Accel.kext"], "script": "install-gpu.sh",
     "run": {"check": "MODE=dry-run bash install-gpu.sh", "install": "MODE=install bash install-gpu.sh"}, "reboot": True},
    {"step": "audio-applehda", "order": 20, "group": "audio", "choice": 2, "recommended": True,
     "what": "Audio option 2 (recommended): AppleHDA on the root volume",
     "artifacts": ["audio-applehda.sh"], "script": "audio-applehda.sh",
     "run": {"check": "MODE=dry-run bash audio-applehda.sh", "install": "MODE=install bash audio-applehda.sh"},
     "reboot": True, "needs": ["FileVault off"], "rerun_after_macos_update": True,
     "notes": ["Revert: MODE=revert boots the last sealed snapshot"]},
    {"step": "audio-voodoohda", "order": 21, "group": "audio", "choice": 1, "what": "Audio option 1 (fallback): VoodooHDA, NO HDMI audio",
     "artifacts": ["audio-voodoohda.sh"], "script": "audio-voodoohda.sh",
     "run": {"check": "MODE=dry-run bash audio-voodoohda.sh", "install": "MODE=install bash audio-voodoohda.sh",
             "revert": "MODE=revert bash audio-voodoohda.sh", "status": "MODE=status bash audio-voodoohda.sh"},
     "reboot": True, "needs": ["audio option 2 not installed"]},
]


def make_release(dry_run=False, tamper=None, v011=False):
    """A manifest + its files, as a dict {file: bytes}. v011: the 0.1.1 additions (post-install scripts and their
    machine-readable steps, Ath10kMac's baked-in country, applbkl in the manifest's DeviceProperties)."""
    arts, blobs = [], {}
    for art, data in (
            _kext("PG106.kext", "PG106", 100, hardware=[GPU]),
            _kext("PG106FB.kext", "PG106FB", 110, requires=["PG106.kext"], hardware=[GPU]),
            _kext("NMHDA.kext", "NMHDA", 120, requires=["Lilu.kext", "AppleALC.kext"], minimum="24.0.0",
                  hardware=[{"bus": "pci", "vendor": "10de", "device": "10f1"}]),
            _kext("QCARomePatchRAM.kext", "QCARomePatchRAM", 130, requires=["Lilu.kext", "BlueToolFixup.kext"], minimum="21.0.0", hardware=[BTH]),
            _kext("Ath10kMac.kext", "Ath10kMac", 140, hardware=[WIFI], maximum="25.99.99", conflicts=["itlwm.kext"]),
            _kext("AirportAth10k.kext", "AirportAth10k", 145, requires=["Ath10kMac.kext"], hardware=[WIFI], maximum="25.99.99"),
            _file("SSDT-PG106-HDAU.aml", "ssdt", "opencore", b"SSDT-hdau",
                  acpi={"order": 100, "Path": "SSDT-PG106-HDAU.aml", "Enabled": True, "Comment": "hdau"}, hardware=[GPU]),
            _file("SSDT-HS07-UPC.aml", "ssdt", "opencore", b"SSDT-hs07",
                  acpi={"order": 110, "Path": "SSDT-HS07-UPC.aml", "Enabled": True, "Comment": "hs07"}, hardware=[BTH]),
            _file("PG106Accel.kext.zip", "kext", "library-extensions", b"accel-zip", hardware=[GPU]),
            _file("NVIDIA-firmware-LICENCE.txt", "firmware-licence", "stick-only", b"licence")):
        arts.append(art)
        blobs[art["file"]] = data
    if v011:
        next(a for a in arts if a["name"] == "Ath10kMac.kext")["personality"] = {"Ath10kCountryCode": {"string": "IN"}}
        for name in ("audio-voodoohda.sh", "audio-applehda.sh", "gpu-apps.sh"):   # gpu-apps.sh: a script in no step
            art, data = _file(name, "script", "stick-only", b"#!/bin/bash\n# " + name.encode() + b"\n", built_by="copy",
                              status=f"{name} test script; second clause")
            arts.append(art)
            blobs[art["file"]] = data
        art, data = _file("POST-INSTALL.md", "doc", "stick-only", b"# after macOS 26 is installed\n", built_by="generated",
                          status="the post-install steps in order")
        arts.append(art)
        blobs[art["file"]] = data
    if tamper:
        blobs[tamper] = blobs[tamper] + b"x"
    manifest = {
        "schema": 1, "repo": "akshaynexus/macos_helios300",
        "release": {"version": "0.1.0", "tag": "v0.1.0", "commit": "0" * 40, "dirty": False, "pre_release": True,
                    "dry_run": dry_run, "built": "2026-11-08T00:00:00Z", "built_on": "test", "sources": {}},
        "machine": {"name": helios300.NAME, "match": {"dmi_product": ["Predator G3-571"], "pci": [GPU]}},
        "macos": {"min_kernel": "25.0.0", "max_kernel": "25.99.99"},
        "artifacts": arts, "external_kexts": [],
        "config": {
            "boot_args": [],
            "nvram_add": {BOOT: {"bluetoothExternalDongleFailed": {"data": "00"},
                                 "bluetoothInternalControllerInfo": {"data": "00" * 14}}},
            "nvram_delete": {BOOT: ["bluetoothExternalDongleFailed", "bluetoothInternalControllerInfo"]},
            "device_properties": {"PciRoot(0x0)/Pci(0x1,0x0)/Pci(0x0,0x0)": {"hda-gfx": {"string": "onboard-2"}},
                                  "PciRoot(0x0)/Pci(0x1,0x0)/Pci(0x0,0x1)": {"hda-gfx": {"string": "onboard-2"}}},
            "forbidden": {"kexts": ["NVRM.kext", "NVRMFB.kext", "NVAccel.kext", "NVRMAGDC.kext"], "boot_args": ["-wegnoegpu"],
                          "device_properties": {"PciRoot(0x0)/Pci(0x1,0x0)/Pci(0x0,0x0)": ["disable-gpu"]},
                          "acpi": ["SSDT-Disable_GPU_PEG0.aml"]}},
        "post_install": [{"step": "pg106accel", "what": "PG106Accel to /Library/Extensions", "artifacts": ["PG106Accel.kext"]}],
    }
    if v011:
        manifest["post_install"] = copy.deepcopy(POST_011)
        manifest["config"]["device_properties"][helios300.IGPU_PATH] = {"applbkl": {"data": "01000000"}}
    return manifest, blobs


def release_dir(**kw):
    m, blobs = make_release(**kw)
    d = tempfile.mkdtemp()
    for f, data in blobs.items():
        with open(os.path.join(d, f), "wb") as fh:
            fh.write(data)
    with open(os.path.join(d, "manifest.json"), "w") as fh:
        json.dump(m, fh)
    return os.path.join(d, "manifest.json")


def engine_config():
    """What the engine writes for the Helios without the release (its own kexts, the dGPU disabled)."""
    entry = lambda n: {"Arch": "Any", "BundlePath": n, "Comment": "", "Enabled": True, "ExecutablePath": "Contents/MacOS/" + n[:-5],  # noqa: E731
                       "MaxKernel": "", "MinKernel": "", "PlistPath": "Contents/Info.plist"}
    return {"ACPI": {"Add": [{"Comment": "", "Enabled": True, "Path": "SSDT-Disable_GPU_PEG0.aml"}], "Delete": [], "Patch": []},
            "Booter": {"Quirks": {}, "MmioWhitelist": []},
            "DeviceProperties": {"Add": {"PciRoot(0x0)/Pci(0x1,0x0)/Pci(0x0,0x0)": {"disable-gpu": True}}},
            "Kernel": {"Add": [entry(n) for n in ("Lilu.kext", "VirtualSMC.kext", "WhateverGreen.kext", "AppleALC.kext",
                                                  "BlueToolFixup.kext", "itlwm.kext", "NVRM.kext")], "Block": [], "Patch": [], "Quirks": {}},
            "Misc": {}, "NVRAM": {"Add": {BOOT: {"boot-args": "-v -wegnoegpu"}}, "Delete": {BOOT: []}},
            "UEFI": {"Drivers": [], "Quirks": {}}, "PlatformInfo": {"Generic": {}}}


def run_apply(manifest_path, cfg=None, macos="25.0.0", hardware=None):
    t = tempfile.mkdtemp()
    oc = os.path.join(t, "EFI", "OC")
    os.makedirs(os.path.join(oc, "Kexts"))
    path = os.path.join(oc, "config.plist")
    with open(path, "wb") as fh:
        plistlib.dump(cfg or engine_config(), fh)
    rel, why = helios300.load_release(env={helios300.ENV_MANIFEST: manifest_path})
    changes = []
    helios300.apply(path, rel, macos, hardware or report(), lambda r, b, a, w: changes.append({"rule": r, "before": b, "after": a, "why": w}))
    with open(path, "rb") as fh:
        return plistlib.load(fh), changes, t


class Detect(unittest.TestCase):
    def test_the_owners_scan_is_the_helios(self):
        hit = helios300.detect(report())
        self.assertEqual(hit["name"], "Acer Predator Helios 300 G3-571")
        self.assertEqual(hit["parts"], {"dgpu": True, "igpu": True, "wifi": True, "bt": True, "codec": True, "hdmi_codec": True})

    def test_another_gtx_1060_laptop_is_not(self):
        r = report()
        r["Motherboard"]["Name"] = "ACER NITRO AN515-51"
        self.assertIsNone(helios300.detect(r))

    def test_a_g3_571_with_another_gpu_subsystem_is_not(self):
        r = report()
        for g in r["GPU"].values():
            if g["Device ID"] == "10DE-1C20":
                g["Subsystem ID"] = "11111025"
        self.assertIsNone(helios300.detect(r))

    def test_the_gtx_1060_is_not_a_nullmoth_card(self):
        # nullmoth's rules (NVRM stack, nvfb/nvaccel, SIP 0x0A43, the package on the stick) need a card in the
        # Turing-or-newer table; the GP106 is not in it, so none of them can fire for this laptop.
        self.assertEqual(nullmoth.cards(report()), {})


class Manifest(unittest.TestCase):
    def test_dry_run_manifest_only_when_named_locally(self):
        m, _ = make_release(dry_run=True)
        helios300.check_manifest(copy.deepcopy(m), local=True)
        with self.assertRaises(helios300.ReleaseError):
            helios300.check_manifest(m, local=False)

    def test_files_must_come_from_the_release(self):
        m, _ = make_release()
        m["artifacts"][0]["url"] = "https://example.com/PG106.kext.zip"
        with self.assertRaises(helios300.ReleaseError):
            helios300.check_manifest(m)

    def test_other_repo_refused(self):
        m, _ = make_release()
        m["repo"] = "someone/else"
        with self.assertRaises(helios300.ReleaseError):
            helios300.check_manifest(m)

    def test_no_pin_and_no_published_release(self):
        with mock.patch.object(helios300, "pin", return_value={"tag": None, "manifest_sha256": None}):
            rel, why = helios300.load_release(fetch=lambda u: b"[]", cache_dir=tempfile.mkdtemp(), env={})
        self.assertIsNone(rel)
        self.assertIn("no macos_helios300 release has been published", why)

    def test_no_pin_uses_the_latest_release_including_pre_releases(self):
        m, blobs = make_release()
        raw = json.dumps(m).encode()
        listing = [{"tag_name": "v0.2.0", "draft": True, "assets": [{"name": "manifest.json"}]},
                   {"tag_name": "v0.1.0", "prerelease": True, "assets": [{"name": "manifest.json"}, {"name": "PG106.kext.zip"}]}]
        urls = []

        def fetch(url):
            urls.append(url)
            if url == helios300.API_RELEASES:
                return json.dumps(listing).encode()
            return raw if url.endswith("/v0.1.0/manifest.json") else blobs[url.rsplit("/", 1)[1]]
        cache = tempfile.mkdtemp()
        with mock.patch.object(helios300, "pin", return_value={"tag": None, "manifest_sha256": None}):
            rel, _ = helios300.load_release(fetch=fetch, cache_dir=cache, env={})
            self.assertEqual((rel.version, rel.source["pinned"], rel.source["tag"]), ("0.1.0", False, "v0.1.0"))
            rel.blob(rel.artifact("PG106.kext"))
            # offline afterwards: the newest cached release, no listing needed
            rel2, _ = helios300.load_release(fetch=None, cache_dir=cache, env={})
        self.assertEqual(rel2.version, "0.1.0")
        rel2.blob(rel2.artifact("PG106.kext"))
        self.assertEqual(urls[0], helios300.API_RELEASES)

    def test_listing_fails_offline_falls_back_to_cache(self):
        def boom(url):
            raise OSError("network down")
        with mock.patch.object(helios300, "pin", return_value={"tag": None, "manifest_sha256": None}):
            rel, why = helios300.load_release(fetch=boom, cache_dir=tempfile.mkdtemp(), env={})
        self.assertIsNone(rel)
        self.assertIn("could not be listed", why)

    def test_a_release_directory_is_used_as_is(self):
        d = os.path.dirname(release_dir())
        rel, _ = helios300.load_release(fetch=lambda u: self.fail("no network for a local release"), manifest=d, env={})
        self.assertEqual(rel.source["kind"], "local")
        rel.blob(rel.artifact("PG106.kext"))

    def test_a_named_tag_must_look_like_one(self):
        with self.assertRaises(helios300.ReleaseError):
            helios300.load_release(fetch=lambda u: b"", release="main; rm -rf /", env={})

    def test_pinned_manifest_checked_and_cached_for_offline_use(self):
        m, blobs = make_release()
        raw = json.dumps(m).encode()
        cache = tempfile.mkdtemp()
        fetched = []

        def fetch(url):
            fetched.append(url)
            return raw if url.endswith("manifest.json") else blobs[url.rsplit("/", 1)[1]]
        pinned = {"tag": "v0.1.0", "manifest_sha256": hashlib.sha256(raw).hexdigest()}
        with mock.patch.object(helios300, "pin", return_value=pinned):
            rel, _ = helios300.load_release(fetch=fetch, cache_dir=cache, env={})
            rel.blob(rel.artifact("PG106.kext"))
            self.assertEqual(len(fetched), 2)
            rel2, _ = helios300.load_release(fetch=None, cache_dir=cache, env={})   # offline: the cache alone
            rel2.blob(rel2.artifact("PG106.kext"))
        self.assertEqual(rel2.version, "0.1.0")
        self.assertEqual(len(fetched), 2)

    def test_a_manifest_that_is_not_the_pinned_one_is_refused(self):
        m, _ = make_release()
        with mock.patch.object(helios300, "pin", return_value={"tag": "v0.1.0", "manifest_sha256": "0" * 64}):
            with self.assertRaises(helios300.ReleaseError):
                helios300.load_release(fetch=lambda u: json.dumps(m).encode(), cache_dir=tempfile.mkdtemp(), env={})

    def test_a_changed_artifact_is_refused(self):
        path = release_dir(tamper="PG106.kext.zip")
        with self.assertRaises(helios300.ReleaseError):
            run_apply(path)


class Apply(unittest.TestCase):
    def setUp(self):
        self.cfg, self.changes, self.root = run_apply(release_dir())
        self.rules = {c["rule"] for c in self.changes}
        self.kexts = [k["BundlePath"] for k in self.cfg["Kernel"]["Add"]]

    def test_kexts_follow_the_engines_in_manifest_order(self):
        ours = ["PG106.kext", "PG106FB.kext", "NMHDA.kext", "QCARomePatchRAM.kext", "Ath10kMac.kext", "AirportAth10k.kext"]
        self.assertEqual(self.kexts[-6:], ours)
        for name in ours:
            entry = next(k for k in self.cfg["Kernel"]["Add"] if k["BundlePath"] == name)
            self.assertTrue(entry["Enabled"])
            self.assertTrue(os.path.isfile(os.path.join(self.root, "EFI", "OC", "Kexts", name, *entry["ExecutablePath"].split("/"))))
        ath = next(k for k in self.cfg["Kernel"]["Add"] if k["BundlePath"] == "Ath10kMac.kext")
        self.assertEqual((ath["MinKernel"], ath["MaxKernel"], ath["Arch"]), ("25.0.0", "25.99.99", "x86_64"))
        self.assertIn("helios-kexts", self.rules)

    def test_dependencies_load_first(self):
        idx = {k: i for i, k in enumerate(self.kexts)}
        self.assertLess(idx["Lilu.kext"], idx["NMHDA.kext"])
        self.assertLess(idx["AppleALC.kext"], idx["NMHDA.kext"])
        self.assertLess(idx["BlueToolFixup.kext"], idx["QCARomePatchRAM.kext"])
        self.assertLess(idx["PG106.kext"], idx["PG106FB.kext"])
        self.assertLess(idx["Ath10kMac.kext"], idx["AirportAth10k.kext"])

    def test_the_gpu_is_not_disabled_or_hidden(self):
        args = self.cfg["NVRAM"]["Add"][BOOT]["boot-args"].split()
        self.assertNotIn("-wegnoegpu", args)
        self.assertNotIn("disable-gpu", self.cfg["DeviceProperties"]["Add"]["PciRoot(0x0)/Pci(0x1,0x0)/Pci(0x0,0x0)"])
        off = next(x for x in self.cfg["ACPI"]["Add"] if x["Path"] == "SSDT-Disable_GPU_PEG0.aml")
        self.assertFalse(off["Enabled"])
        self.assertIn("helios-forbidden", self.rules)

    def test_no_turing_stack(self):
        nvrm = next(k for k in self.cfg["Kernel"]["Add"] if k["BundlePath"] == "NVRM.kext")
        self.assertFalse(nvrm["Enabled"])

    def test_conflicting_wifi_driver_off(self):
        itlwm = next(k for k in self.cfg["Kernel"]["Add"] if k["BundlePath"] == "itlwm.kext")
        self.assertFalse(itlwm["Enabled"])

    def test_hdmi_audio_pairing_and_bluetooth_nvram(self):
        dp = self.cfg["DeviceProperties"]["Add"]
        self.assertEqual(dp["PciRoot(0x0)/Pci(0x1,0x0)/Pci(0x0,0x0)"]["hda-gfx"], "onboard-2")
        self.assertEqual(dp["PciRoot(0x0)/Pci(0x1,0x0)/Pci(0x0,0x1)"]["hda-gfx"], "onboard-2")
        nv = self.cfg["NVRAM"]["Add"][BOOT]
        self.assertEqual(nv["bluetoothExternalDongleFailed"], b"\x00")
        self.assertEqual(nv["bluetoothInternalControllerInfo"], bytes(14))
        self.assertIn("bluetoothInternalControllerInfo", self.cfg["NVRAM"]["Delete"][BOOT])

    def test_brightness_whatevergreen_backlight_patch_forced_on(self):
        # The GTX 1060 stays visible for PG106, so WhateverGreen's applbkl default (detect: IGPU-only setups) would skip
        # AppleBacklight and the CMN panel would get no 'brightness' parameter (no slider on macOS 26.7.2).
        igpu = self.cfg["DeviceProperties"]["Add"][helios300.IGPU_PATH]
        self.assertEqual(igpu["applbkl"], b"\x01\x00\x00\x00")
        self.assertIn("helios-backlight", self.rules)

    def test_backlight_rule_is_idempotent(self):
        cfg = engine_config()
        cfg["DeviceProperties"]["Add"][helios300.IGPU_PATH] = {"AAPL,ig-platform-id": bytes.fromhex("00001b59"),
                                                              "applbkl": helios300.APPLBKL_ON}
        cfg2, changes, _ = run_apply(release_dir(), cfg=cfg)
        igpu = cfg2["DeviceProperties"]["Add"][helios300.IGPU_PATH]
        self.assertEqual(igpu["AAPL,ig-platform-id"], bytes.fromhex("00001b59"))
        self.assertEqual(igpu["applbkl"], helios300.APPLBKL_ON)
        self.assertNotIn("helios-backlight", {c["rule"] for c in changes})

    def test_ssdts_in_order(self):
        paths = [x["Path"] for x in self.cfg["ACPI"]["Add"]]
        self.assertEqual(paths[-2:], ["SSDT-PG106-HDAU.aml", "SSDT-HS07-UPC.aml"])
        self.assertTrue(os.path.isfile(os.path.join(self.root, "EFI", "OC", "ACPI", "SSDT-HS07-UPC.aml")))

    def test_post_install_kit_on_the_stick(self):
        kit = os.path.join(self.root, "EFI", "Helios300")
        self.assertEqual(sorted(os.listdir(kit)), ["NVIDIA-firmware-LICENCE.txt", "PG106Accel.kext.zip", "README.txt", "manifest.json"])
        self.assertIn("helios-post-install", self.rules)

    def test_local_manifest_is_named_as_unpinned(self):
        rel = next(c for c in self.changes if c["rule"] == "helios-release")
        self.assertIn("LOCAL, UNPINNED", rel["after"])


class ApplyLimits(unittest.TestCase):
    def test_macos_15_gets_none_of_it(self):
        cfg, changes, _ = run_apply(release_dir(), macos="24.6.0")
        self.assertNotIn("PG106.kext", [k["BundlePath"] for k in cfg["Kernel"]["Add"]])
        self.assertIn("helios-macos-range", {c["rule"] for c in changes})
        self.assertNotIn("helios-backlight", {c["rule"] for c in changes})   # the engine hides the dGPU there

    def test_a_replaced_wifi_card_gets_no_wifi_kexts(self):
        r = report()
        r["Network"] = {n: p for n, p in r["Network"].items() if p["Device ID"] != "168C-003E"}
        cfg, changes, _ = run_apply(release_dir(), hardware=r)
        kexts = [k["BundlePath"] for k in cfg["Kernel"]["Add"]]
        self.assertNotIn("Ath10kMac.kext", kexts)
        self.assertNotIn("AirportAth10k.kext", kexts)
        self.assertIn("helios-skipped", {c["rule"] for c in changes})

    def test_no_backlight_property_without_the_hd_630(self):
        hw = copy.deepcopy(report())
        hw["GPU"] = {k: v for k, v in hw["GPU"].items() if v.get("Device ID") != "8086-591B"}
        cfg, changes, _ = run_apply(release_dir(), hardware=hw)
        self.assertNotIn(helios300.IGPU_PATH, cfg["DeviceProperties"]["Add"])
        self.assertNotIn("helios-backlight", {c["rule"] for c in changes})

    def test_missing_dependency_skips_the_kext(self):
        cfg = engine_config()
        cfg["Kernel"]["Add"] = [k for k in cfg["Kernel"]["Add"] if k["BundlePath"] != "BlueToolFixup.kext"]
        cfg, changes, _ = run_apply(release_dir(), cfg=cfg)
        self.assertNotIn("QCARomePatchRAM.kext", [k["BundlePath"] for k in cfg["Kernel"]["Add"]])


class Release011(unittest.TestCase):
    """0.1.1 manifests: post-install scripts with machine-readable steps, the Wi-Fi country in Ath10kMac,
    applbkl from the manifest."""

    def setUp(self):
        self.cfg, self.changes, self.root = run_apply(release_dir(v011=True))
        self.rules = [c["rule"] for c in self.changes]
        self.kit = os.path.join(self.root, "EFI", "Helios300")
        with open(os.path.join(self.kit, "README.txt"), encoding="utf-8") as fh:
            self.readme = fh.read()

    def test_scripts_go_on_the_stick(self):
        self.assertEqual(sorted(os.listdir(self.kit)), ["NVIDIA-firmware-LICENCE.txt", "PG106Accel.kext.zip", "POST-INSTALL.md", "README.txt",
                                                        "audio-applehda.sh", "audio-voodoohda.sh", "gpu-apps.sh", "manifest.json"])

    def test_every_script_and_doc_artifact_is_copied_and_listed(self):
        # every non-OpenCore artifact goes in the kit, scripts outside any post_install step and docs included, and
        # the README names each file with what it is
        with open(os.path.join(self.kit, "manifest.json"), encoding="utf-8") as fh:
            m = json.load(fh)
        files = self.readme[self.readme.index("Files in this folder:"):]
        for a in m["artifacts"]:
            if a["install"] == "opencore":
                self.assertFalse(os.path.exists(os.path.join(self.kit, a["file"])), a["file"])
                self.assertNotIn(a["file"], files)
                continue
            self.assertTrue(os.path.isfile(os.path.join(self.kit, a["file"])), a["file"])
            self.assertRegex(files, r"\n  " + re.escape(a["file"]) + r" +" + re.escape(a["kind"]) + ": ")
        self.assertIn("gpu-apps.sh                        script: gpu-apps.sh test script\n", files)
        self.assertIn("POST-INSTALL.md                    doc: the post-install steps in order", files)
        self.assertIn("POST-INSTALL.md, from the release, has the same steps", self.readme)
        self.assertIn("manifest.json", files)

    def test_option_2_removes_option_1_after_the_snapshot(self):
        self.assertNotIn("removes option 1 first", self.readme)
        self.assertIn("only once the new system snapshot is in place", self.readme)

    def test_readme_lists_the_steps_with_their_commands(self):
        self.assertIn("AUDIO: do ONE of these 2 options.", self.readme)
        # the manifest's order: the recommended option 2 first, the fallback option 1 after it
        self.assertLess(self.readme.index("[gpu]"), self.readme.index("[audio-applehda] option 2 (recommended)"))
        self.assertLess(self.readme.index("[audio-applehda] option 2 (recommended)"), self.readme.index("[audio-voodoohda] option 1"))
        self.assertNotIn("[audio-voodoohda] option 1 (recommended)", self.readme)
        for cmd in ("MODE=dry-run bash audio-voodoohda.sh", "MODE=install bash audio-voodoohda.sh",
                    "MODE=revert bash audio-voodoohda.sh", "MODE=install bash audio-applehda.sh"):
            self.assertIn(cmd, self.readme)
        self.assertIn("needs:   FileVault off", self.readme)
        self.assertIn("run it again after every macOS update", self.readme)
        self.assertIn("- Revert: MODE=revert boots the last sealed snapshot", self.readme)

    def test_a_step_whose_script_is_not_released_is_done_by_hand(self):
        gpu = self.readme[self.readme.index("[gpu]"):self.readme.index("[audio-applehda]")]
        self.assertIn("install-gpu.sh is not in this release: do it by hand", gpu)
        self.assertNotIn("MODE=install bash install-gpu.sh", gpu)

    def test_readme_describes_both_audio_options(self):
        self.assertIn("NO HDMI audio", self.readme)
        self.assertIn("FileVault must be OFF", self.readme)
        self.assertIn("run it again after every update", self.readme)

    def test_wifi_country_from_the_kext_is_reported(self):
        rule = next(c for c in self.changes if c["rule"] == "helios-wifi-country")
        self.assertIn("IN", rule["after"])
        self.assertNotIn("ath10k_cc", self.cfg["NVRAM"]["Add"][BOOT].get("boot-args", ""))

    def test_applbkl_from_the_manifest_once(self):
        self.assertEqual(self.cfg["DeviceProperties"]["Add"][helios300.IGPU_PATH]["applbkl"], helios300.APPLBKL_ON)
        self.assertEqual(self.rules.count("helios-backlight"), 1)

    def test_manifest_igpu_properties_need_the_hd_630(self):
        hw = copy.deepcopy(report())
        hw["GPU"] = {k: v for k, v in hw["GPU"].items() if v.get("Device ID") != "8086-591B"}
        cfg, changes, _ = run_apply(release_dir(v011=True), hardware=hw)
        self.assertNotIn(helios300.IGPU_PATH, cfg["DeviceProperties"]["Add"])
        self.assertNotIn("helios-backlight", {c["rule"] for c in changes})

    def test_no_country_rule_without_wifi_or_for_an_old_manifest(self):
        r = report()
        r["Network"] = {n: p for n, p in r["Network"].items() if p["Device ID"] != "168C-003E"}
        _, changes, _ = run_apply(release_dir(v011=True), hardware=r)
        self.assertNotIn("helios-wifi-country", {c["rule"] for c in changes})
        _, changes, _ = run_apply(release_dir())
        self.assertNotIn("helios-wifi-country", {c["rule"] for c in changes})

    def test_old_manifest_steps_still_listed(self):
        _, _, root = run_apply(release_dir())
        with open(os.path.join(root, "EFI", "Helios300", "README.txt"), encoding="utf-8") as fh:
            self.assertIn("[pg106accel] PG106Accel to /Library/Extensions\n    files: PG106Accel.kext", fh.read())


class KeepDevices(unittest.TestCase):
    def disabled(self):
        return {"GPU: NVIDIA Corporation GP106M [GeForce GTX 1060 Mobile]": {"Device ID": "10DE-1C20"},
                "Network: Qualcomm Atheros QCA6174 802.11ac Wireless Network Adapter": {"Device ID": "168C-003E"},
                "Sound: Nvidia GPU 84 HDMI/DP (HDMI 0)": {"Device ID": "10DE-0084"},
                "Bluetooth: Lite-On Technology Corp.": {"Device ID": "04CA-3016"},
                "SD Controller: something": {"Device ID": "1234-5678"}}

    def test_release_keeps_what_it_drives(self):
        rel, _ = helios300.load_release(env={helios300.ENV_MANIFEST: release_dir()})
        left, kept = helios300.keep_devices(self.disabled(), rel, "25.0.0", [])
        self.assertEqual(list(left), ["SD Controller: something"])
        self.assertEqual(len(kept), 4)

    def test_without_a_release_the_engine_decides(self):
        left, kept = helios300.keep_devices(self.disabled(), None, "25.0.0", [])
        self.assertEqual(len(left), 5)
        self.assertEqual(kept, [])

    def test_macos_15_keeps_the_engines_choice(self):
        rel, _ = helios300.load_release(env={helios300.ENV_MANIFEST: release_dir()})
        left, kept = helios300.keep_devices(self.disabled(), rel, "24.6.0", [])
        self.assertEqual(len(left), 5)


class Notes(unittest.TestCase):
    def res(self, smbios="MacBookPro14,3", darwin="25.0.0"):
        return engine.BuildResult(ok=True, out_dir="", macos_version=darwin, smbios=smbios)

    def test_pending_release_is_said(self):
        rules = {r[0] for r in helios300.notes(self.res(), None, "no release", [])}
        self.assertIn("helios-release-pending", rules)
        self.assertIn("helios-oclp-post-install", rules)
        self.assertNotIn("helios-cpufriend", rules)

    def test_audio_options_are_named(self):
        after = next(r[2] for r in helios300.notes(self.res(), None, "x", []) if r[0] == "helios-oclp-post-install")
        for word in ("VoodooHDA", "no HDMI audio", "AppleHDA", "recommended", "fallback", "FileVault off",
                     "after every macOS update", "Web Drivers"):
            self.assertIn(word, after)

    def test_spoofed_smbios_asks_for_cpufriend(self):
        rules = {r[0] for r in helios300.notes(self.res(smbios="MacBookPro16,1"), None, "x", [])}
        self.assertIn("helios-cpufriend", rules)


class PolicyPass(unittest.TestCase):
    def test_no_nullmoth_rule_fires(self):
        t = tempfile.mkdtemp()
        path = os.path.join(t, "config.plist")
        with open(path, "wb") as fh:
            plistlib.dump(engine_config(), fh)
        res = engine.BuildResult(ok=True, out_dir=t, macos_version="25.0.0", hardware=report(), raw_hardware=report())
        changes = policy.apply(path, res, engine.Policy(tahoe_audio="applealc"))
        self.assertFalse([c for c in changes if c["rule"].startswith("nullmoth")])
        with open(path, "rb") as fh:
            args = plistlib.load(fh)["NVRAM"]["Add"][BOOT].get("boot-args", "").split()
        for a in nullmoth.BOOT_ARGS:
            self.assertNotIn(a, args)


class Cli(unittest.TestCase):
    def policy_for(self, *argv):
        from p1401 import __main__ as cli  # noqa: PLC0415
        seen = {}

        def fake_build(report, acpi, out, pol, echo=False, download=True):
            seen["policy"], seen["download"] = pol, download
            return engine.BuildResult(ok=True, out_dir=out)
        with mock.patch.object(engine, "build", fake_build), mock.patch("sys.stdout", io.StringIO()):
            cli.main(["build", REPORT, "acpi", tempfile.mkdtemp(), *argv])
        return seen["policy"]

    def test_default_is_the_published_release(self):
        pol = self.policy_for()
        self.assertEqual((pol.helios_release, pol.helios_manifest, pol.helios_build_from_source), ("", "", False))

    def test_release_dir_and_manifest(self):
        self.assertEqual(self.policy_for("--release-dir", "/r").helios_manifest, "/r")
        self.assertEqual(self.policy_for("--manifest", "/r/manifest.json").helios_manifest, "/r/manifest.json")
        self.assertEqual(self.policy_for("--release", "latest").helios_release, "latest")

    def test_build_from_source(self):
        pol = self.policy_for("--build-from-source", "--ref", "v0.1.0")
        self.assertTrue(pol.helios_build_from_source)
        self.assertEqual(pol.helios_ref, "v0.1.0")


class BuildFromSource(unittest.TestCase):
    """Source mode only drives macos_helios300's own tools/release.sh; nothing is built by 1401."""

    def run_mode(self, ref, release_rc=0, write_manifest=True, env=None):
        work = tempfile.mkdtemp()
        calls = []

        def run(cmd, cwd=None, **kw):
            calls.append((cmd, cwd))
            if cmd[0] == "git":
                if cmd[1] == "clone":
                    os.makedirs(os.path.join(cmd[-1], ".git"))
                out = "0123456789abcdef0123456789abcdef01234567" if cmd[1] == "rev-parse" else ""
                return mock.Mock(returncode=0, stdout=out, stderr="")
            dist = cmd[cmd.index("--dist") + 1]
            if release_rc == 0 and write_manifest:
                os.makedirs(dist, exist_ok=True)
                with open(os.path.join(dist, "manifest.json"), "w") as fh:
                    fh.write("{}")
            return mock.Mock(returncode=release_rc)
        result = helios300.build_from_source(ref, work, run=run, env=env or {})
        return result, calls, work

    def test_runs_release_sh_with_the_tag_version(self):
        manifest, calls, work = self.run_mode("v0.1.0")
        git = [c[0][1] for c in calls if c[0][0] == "git"]
        self.assertEqual(git[:2], ["clone", "fetch"])
        self.assertIn("checkout", git)
        self.assertIn(["git", "submodule", "update", "--init", "--quiet"], [c[0] for c in calls])
        release = [c for c in calls if c[0][0] == "bash"]
        self.assertEqual(len(release), 1)
        cmd, cwd = release[0]
        src = os.path.join(work, "macos_helios300")
        self.assertEqual(cmd, ["bash", os.path.join(src, "tools", "release.sh"), "--version", "0.1.0",
                               "--dist", os.path.join(work, "dist")])
        self.assertEqual(cwd, src)
        self.assertEqual(manifest, os.path.join(work, "dist", "manifest.json"))

    def test_a_commit_gets_a_source_version(self):
        _, calls, _ = self.run_mode("main")
        cmd = next(c[0] for c in calls if c[0][0] == "bash")
        self.assertEqual(cmd[cmd.index("--version") + 1], "0.0.0-src.0123456789ab")

    def test_iaf2_is_passed_through(self):
        _, calls, _ = self.run_mode("v0.1.0", env={"HELIOS_IAF2": "/x/IOAcceleratorFamily2"})
        cmd = next(c[0] for c in calls if c[0][0] == "bash")
        self.assertEqual(cmd[-2:], ["--iaf2", "/x/IOAcceleratorFamily2"])

    def run_env(self, env):
        seen = []

        def run(cmd, cwd=None, **kw):
            if cmd[0] == "git":
                if cmd[1] == "clone":
                    os.makedirs(os.path.join(cmd[-1], ".git"))
                return mock.Mock(returncode=0, stdout="0" * 40 if cmd[1] == "rev-parse" else "", stderr="")
            seen.append(kw.get("env") or {})
            dist = cmd[cmd.index("--dist") + 1]
            os.makedirs(dist, exist_ok=True)
            with open(os.path.join(dist, "manifest.json"), "w") as fh:
                fh.write("{}")
            return mock.Mock(returncode=0)
        with mock.patch("builtins.print"):
            helios300.build_from_source("v0.1.1", tempfile.mkdtemp(), run=run, env=env)
        return seen[0]

    def test_without_kernel_collections_the_audit_is_skipped(self):
        # release.sh refuses a real release without the target Mac's KCs; a local source build goes on, recorded.
        self.assertEqual(self.run_env({}).get("HELIOS_KC_AUDIT"), "skip")

    def test_kernel_collections_are_used_when_named(self):
        env = self.run_env({"HELIOS_KC_DIR": "/kc", "HELIOS_WIFI_COUNTRY": "DE"})
        self.assertNotIn("HELIOS_KC_AUDIT", env)
        self.assertEqual((env["HELIOS_KC_DIR"], env["HELIOS_WIFI_COUNTRY"]), ("/kc", "DE"))

    def test_missing_toolchain_says_where_to_get_it(self):
        with self.assertRaises(helios300.ReleaseError) as e:
            self.run_mode("v0.1.0", release_rc=77)
        self.assertIn("cross toolchain", str(e.exception))
        self.assertIn("docs/MANIFEST.md", str(e.exception))

    def test_failed_build_stops(self):
        with self.assertRaises(helios300.ReleaseError) as e:
            self.run_mode("v0.1.0", release_rc=1)
        self.assertIn("release.sh failed", str(e.exception))

    def test_bad_ref_refused(self):
        with self.assertRaises(helios300.ReleaseError):
            helios300.build_from_source("--upload-pack=x", tempfile.mkdtemp(), run=lambda *a, **k: self.fail("ran"))


@unittest.skipUnless(os.environ.get("A1401_HELIOS_ACPI"), "set A1401_HELIOS_ACPI to the Helios ACPI dump (not in git)")
class FullBuild(unittest.TestCase):
    """The real engine build + ocvalidate for the Helios scan, with A1401_HELIOS_MANIFEST if it is set."""

    def test_full_build(self):
        out = tempfile.mkdtemp()
        r = engine.build(REPORT, os.environ["A1401_HELIOS_ACPI"], out, engine.Policy(macos="25"))
        self.assertTrue(r.ok, r.error)
        self.assertTrue(r.validation.get("ok"), r.validation)
        rules = {c["rule"] for c in r.policy_changes}
        self.assertIn("helios-detected", rules)
        self.assertFalse([x for x in rules if x.startswith("nullmoth")])
        with open(os.path.join(out, "EFI", "OC", "config.plist"), "rb") as fh:
            cfg = plistlib.load(fh)
        self.assertNotIn("-wegnoegpu", cfg["NVRAM"]["Add"][BOOT].get("boot-args", "").split())
        kexts = {k["BundlePath"] for k in cfg["Kernel"]["Add"] if k.get("Enabled")}
        self.assertFalse(kexts & set(helios300.NEVER))
        self.assertTrue({"AppleALC.kext", "VoodooPS2Controller.kext", "VoodooI2C.kext", "RealtekCardReader.kext"} <= kexts)
        layout = cfg["DeviceProperties"]["Add"].get("PciRoot(0x0)/Pci(0x1f,0x3)", {}).get("layout-id")
        self.assertIn(layout, (29, (29).to_bytes(4, "little")))   # the engine writes it as an integer
        # Brightness: PNLF under GFX0 with _UID 16 (Skylake/Kaby Lake profile F16Txxxx, PWM max 0x56C) and -igfxblt (the
        # backlight-registers fix for Kaby Lake on macOS 13.4+).
        with open(os.path.join(out, "EFI", "OC", "ACPI", "SSDT-PNLF.aml"), "rb") as fh:
            pnlf = fh.read()
        self.assertIn(b"_SB_PCI0GFX0PNLF", pnlf)
        self.assertIn(b"_UID\x0a\x10", pnlf)
        self.assertIn("-igfxblt", cfg["NVRAM"]["Add"][BOOT].get("boot-args", "").split())
        if os.environ.get(helios300.ENV_MANIFEST):
            self.assertTrue({"PG106.kext", "AirportAth10k.kext", "QCARomePatchRAM.kext"} <= kexts)
            self.assertEqual(cfg["DeviceProperties"]["Add"][helios300.IGPU_PATH]["applbkl"], helios300.APPLBKL_ON)


class UefiShellTests(unittest.TestCase):
    def test_uefi_shell_added_once_from_the_opencore_download(self):
        from p1401 import helios300 as h
        with tempfile.TemporaryDirectory() as d:
            oc = os.path.join(d, "EFI", "OC"); os.makedirs(os.path.join(oc, "Tools"))
            ock = os.path.join(d, "OCK"); src = os.path.join(ock, "OpenCorePkg", "EFI", "OC", "Tools")
            os.makedirs(src)
            with open(os.path.join(src, "OpenShell.efi"), "wb") as fh:
                fh.write(b"MZshell")
            cfg, ch = {"Misc": {"Tools": []}}, []
            h._add_uefi_shell(cfg, oc, lambda *a: ch.append(a), ock_cache=ock)
            h._add_uefi_shell(cfg, oc, lambda *a: ch.append(a), ock_cache=ock)
            self.assertEqual([t["Path"] for t in cfg["Misc"]["Tools"]], ["OpenShell.efi"])
            self.assertTrue(cfg["Misc"]["Tools"][0]["Enabled"])
            with open(os.path.join(oc, "Tools", "OpenShell.efi"), "rb") as fh:
                self.assertEqual(fh.read(), b"MZshell")
            self.assertEqual(len([c for c in ch if c[0] == "helios-uefi-shell"]), 1)

    def test_no_shell_entry_without_the_file(self):
        from p1401 import helios300 as h
        with tempfile.TemporaryDirectory() as d:
            oc = os.path.join(d, "EFI", "OC"); os.makedirs(oc)
            cfg, ch = {"Misc": {"Tools": []}}, []
            h._add_uefi_shell(cfg, oc, lambda *a: ch.append(a), ock_cache=os.path.join(d, "none"))
            self.assertEqual(cfg["Misc"]["Tools"], [])
            self.assertIn("not added", ch[0][2])


if __name__ == "__main__":
    unittest.main()

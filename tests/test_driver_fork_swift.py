"""The Mac app's side of a driver fork (mac/Sources/DriverSource.swift): the stick manifest nullmoth.stage() writes is
read back by the shipping Swift code, compiled with a small probe. Skipped where there is no Swift compiler."""
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROBE = r'''
import Foundation
let vol = CommandLine.arguments[1]
let es = DriverSource.entries(volumes: vol)
print(es.count)
for e in es { print(e.repo, e.version, e.official, e.signed, e.sha256, DriverSource.warning(e) == nil ? "no-warning" : "warning") }
let t = DriverSource.merged(["ids": ["2D05": "RTX 5060"]], es)
print((t["ids"] as? [String: String] ?? [:]).keys.sorted().joined(separator: ","))
'''


@unittest.skipUnless(shutil.which("swiftc"), "no Swift compiler")
class MacManifest(unittest.TestCase):
    def test_the_mac_app_reads_what_stage_writes(self):
        from p1401 import driver_source as ds, nullmoth  # noqa: PLC0415
        with tempfile.TemporaryDirectory() as t:
            nm = os.path.join(t, "vol", "STICK", "NullMoth")
            os.makedirs(nm)
            data = b"pkg"
            name = "nullmoth-nvidia-2.8.0.tar.gz"
            with open(os.path.join(nm, name), "wb") as fh:
                fh.write(data)
            manifest = {"format": 1, "repo": "someone/fork", "official": False, "release": "v2.8.0",
                        "package": {"name": name, "sha256": hashlib.sha256(data).hexdigest(), "version": "2.8.0"},
                        "signature": "UNSIGNED (allow_unsigned)", "signed": False, "public_key": None,
                        "cards": {"ids": {"fff0": "Fork card", "../x": "bad"}}}
            with open(os.path.join(nm, ds.STICK_MANIFEST), "w") as fh:
                json.dump(manifest, fh)
            bad = os.path.join(t, "vol", "OTHER", "NullMoth")  # a manifest naming a path outside its folder is ignored
            os.makedirs(bad)
            with open(os.path.join(bad, ds.STICK_MANIFEST), "w") as fh:
                json.dump(dict(manifest, package=dict(manifest["package"], name="../../evil.tar.gz")), fh)
            src = os.path.join(t, "main.swift")
            with open(src, "w") as fh:
                fh.write(PROBE)
            exe = os.path.join(t, "probe")
            subprocess.run(["swiftc", str(ROOT / "mac/Sources/DriverSource.swift"), src, "-o", exe], check=True,
                           capture_output=True, timeout=600)
            out = subprocess.run([exe, os.path.join(t, "vol")], check=True, capture_output=True, text=True).stdout.split("\n")
        self.assertEqual(out[0], "1")
        self.assertEqual(out[1].split(), ["someone/fork", "2.8.0", "false", "false", hashlib.sha256(data).hexdigest(), "warning"])
        self.assertEqual(out[2], "2D05,FFF0")
        self.assertEqual(nullmoth.PACKAGE["name"].startswith("nullmoth-nvidia-"), True)


if __name__ == "__main__":
    unittest.main()

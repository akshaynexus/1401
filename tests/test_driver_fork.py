"""A fork of the NullMoth NVIDIA driver as the stick's driver source (p1401/driver_source.py, nullmoth.stage).

No network: every release, SHA256SUMS.txt, signature and package comes from a dict behind a fake fetch()."""
import base64
import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from p1401 import driver_source as ds
from p1401 import nullmoth, release_sig

SEED = hashlib.sha256(b"1401 fork test key, never a release key").digest()
KEY = release_sig.sign(b"", SEED)[1]
OTHER = b"\x02" * 32
REPO = "someone/nvidia-macos-driver"
DL = f"https://github.com/{REPO}/releases/download/v2.8.0/"
NAME = "nullmoth-nvidia-2.8.0.tar.gz"
DATA = b"fork driver package 2.8.0"
SHA = hashlib.sha256(DATA).hexdigest()


def sig(data, seed=SEED):
    return base64.b64encode(release_sig.sign(data, seed)[0])


def bsig(data, version="2.8.0", seed=SEED):
    msg = release_sig.bound_message("nullmoth-nvidia", version, hashlib.sha256(data).hexdigest())
    return base64.b64encode(release_sig.sign(msg, seed)[0])


def release(files, sums=None, api=f"https://api.github.com/repos/{REPO}/releases/latest", tag="v2.8.0", base=DL):
    """{url: bytes} for a release holding `files` ({name: bytes}) and a SHA256SUMS.txt listing `sums` (default: all)."""
    sums = {n: hashlib.sha256(b).hexdigest() for n, b in files.items() if not n.endswith("sig")} if sums is None else sums
    blobs = {base + n: b for n, b in files.items()}
    names = list(files)
    if sums is not False:
        blobs[base + "SHA256SUMS.txt"] = "".join(f"{h}  {n}\n" for n, h in sums.items()).encode()
        names.append("SHA256SUMS.txt")
    blobs[api] = json.dumps({"tag_name": tag, "assets": [{"name": n, "browser_download_url": base + n} for n in names]}).encode()
    return blobs


class Fetch:
    def __init__(self, blobs):
        self.blobs, self.got = blobs, []

    def __call__(self, url, path):
        self.got.append(url)
        if url not in self.blobs:
            raise OSError(f"HTTP 404 {url}")
        with open(path, "wb") as fh:
            fh.write(self.blobs[url])


class Config(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_no_config_is_nullmoths_pinned_package(self):
        env = {"XDG_CONFIG_HOME": self.tmp.name, "HOME": self.tmp.name}
        src = ds.load(env, "linux")
        self.assertIs(src, ds.DEFAULT)
        self.assertTrue(src.builtin and src.official)
        table = {"ids": {"2D05": "x"}, "tested": []}
        self.assertIs(ds.card_table(table, src), table)

    def test_config_folder_per_platform(self):
        self.assertEqual(ds.config_path({"XDG_CONFIG_HOME": "/x"}, "linux"), os.path.join("/x", "NullMoth", "1401", "driver-source.json"))
        self.assertEqual(ds.config_path({"HOME": "/h"}, "linux"), os.path.join("/h", ".config", "NullMoth", "1401", "driver-source.json"))
        self.assertEqual(ds.config_path({"APPDATA": "C:/A"}, "win32"), os.path.join("C:/A", "NullMoth", "1401", "driver-source.json"))
        self.assertEqual(ds.config_path({"HOME": "/h"}, "darwin"),
                         os.path.join("/h", "Library", "Application Support", "NullMoth", "1401", "driver-source.json"))

    def test_repo_spellings(self):
        for s in ("someone/nvidia-macos-driver", "https://github.com/someone/nvidia-macos-driver",
                  "https://github.com/someone/nvidia-macos-driver.git", "github.com/someone/nvidia-macos-driver/"):
            self.assertEqual(ds.parse_repo(s), REPO)
        for s in ("", "someone", "https://gitlab.com/a/b", "a/b/c", "a/..", "-a/b", "a/b c"):
            with self.assertRaises(ds.SourceError, msg=s):
                ds.parse_repo(s)

    def test_a_fork_needs_its_key_or_an_explicit_allow_unsigned(self):
        with self.assertRaisesRegex(ds.SourceError, "allow_unsigned"):
            ds.from_dict({"repo": REPO}, "t")
        self.assertIsNone(ds.from_dict({"repo": REPO, "allow_unsigned": True}, "t").public_key)
        self.assertEqual(ds.from_dict({"repo": REPO, "public_key": KEY}, "t").public_key, KEY)
        with self.assertRaises(ds.SourceError):
            ds.from_dict({"repo": REPO, "public_key": "not a key"}, "t")
        with self.assertRaises(ds.SourceError):
            ds.from_dict({"repo": REPO, "allow_unsigned": "yes"}, "t")
        with self.assertRaisesRegex(ds.SourceError, "unknown"):
            ds.from_dict({"repo": REPO, "public_key": KEY, "skip_checks": True}, "t")

    def test_a_fork_is_never_checked_with_nullmoths_key(self):
        unsigned = ds.from_dict({"repo": REPO, "allow_unsigned": True}, "t")
        self.assertIsNone(unsigned.key(release_sig.PUBLIC_KEY))
        self.assertEqual(ds.from_dict({"repo": REPO, "public_key": KEY}, "t").key(release_sig.PUBLIC_KEY), KEY)
        self.assertEqual(ds.from_dict({"repo": ds.OFFICIAL_REPO}, "t").key(release_sig.PUBLIC_KEY), release_sig.PUBLIC_KEY)

    def test_nullmoths_repo_cannot_be_unsigned_or_take_another_key(self):
        with self.assertRaises(ds.SourceError):
            ds.from_dict({"repo": ds.OFFICIAL_REPO, "allow_unsigned": True}, "t")
        with self.assertRaises(ds.SourceError):
            ds.from_dict({"repo": "NullMoth/NVIDIA-macOS-driver", "public_key": KEY}, "t")
        self.assertTrue(ds.from_dict({"repo": ds.OFFICIAL_REPO, "public_key": release_sig.PUBLIC_KEY}, "t").official)

    def test_a_pinned_package_must_be_in_the_forks_own_release(self):
        good = {"name": NAME, "url": DL + NAME, "sha256": SHA}
        src = ds.from_dict({"repo": REPO, "public_key": KEY, "package": good}, "t")
        self.assertEqual((src.tag, src.package["version"]), ("v2.8.0", "2.8.0"))
        self.assertTrue(src.release_api().endswith(f"/repos/{REPO}/releases/tags/v2.8.0"))
        for bad in ({**good, "url": "https://github.com/nullmoth/nvidia-macos-driver/releases/download/v2.8.0/" + NAME},
                    {**good, "url": "http://github.com/" + REPO + "/releases/download/v2.8.0/" + NAME},
                    {**good, "name": "../evil.tar.gz"}, {**good, "sha256": "00"}):
            with self.assertRaises(ds.SourceError, msg=bad):
                ds.from_dict({"repo": REPO, "public_key": KEY, "package": bad}, "t")
        with self.assertRaisesRegex(ds.SourceError, "another release"):
            ds.from_dict({"repo": REPO, "public_key": KEY, "tag": "v9", "package": good}, "t")

    def test_card_table_merged_or_replaced(self):
        default = {"source": "NVIDIA", "tested": ["2D05"], "ids": {"2D05": "RTX 5060", "2204": "RTX 3090"}}
        path = os.path.join(self.tmp.name, "cards.json")
        with open(path, "w") as fh:
            json.dump({"ids": {"2f04": "Fork card", "2204": "renamed"}, "tested": ["2f04"]}, fh)
        with open(os.path.join(self.tmp.name, "driver-source.json"), "w") as fh:
            json.dump({"repo": REPO, "public_key": KEY, "cards": "cards.json"}, fh)
        src = ds.load({ds.ENV_FILE: os.path.join(self.tmp.name, "driver-source.json")})
        merged = ds.card_table(default, src)
        self.assertEqual(merged["ids"], {"2D05": "RTX 5060", "2204": "renamed", "2F04": "Fork card"})
        self.assertEqual(merged["tested"], ["2D05", "2F04"])
        self.assertEqual(default["ids"], {"2D05": "RTX 5060", "2204": "RTX 3090"})
        rep = ds.card_table(default, ds.from_dict({"repo": REPO, "public_key": KEY, "cards_mode": "replace",
                                                   "cards": {"ids": {"2F04": "Fork card"}}}, "t"))
        self.assertEqual(rep["ids"], {"2F04": "Fork card"})
        with self.assertRaises(ds.SourceError):
            ds.from_dict({"repo": REPO, "public_key": KEY, "cards": {"ids": {"XYZ": "bad"}}}, "t")

    def test_the_merged_table_drives_mark(self):
        table = ds.card_table(nullmoth.OFFICIAL_TABLE, ds.from_dict(
            {"repo": REPO, "public_key": KEY, "cards": {"ids": {"FFF0": "Fork-only card"}}}, "t"))
        card = {"Manufacturer": "NVIDIA", "Device Type": "Discrete GPU", "Device ID": "10DE-FFF0"}
        self.assertFalse(nullmoth.supported(card))
        with mock.patch.object(nullmoth, "TABLE", table):
            rep = {"GPU": {"Fork card": dict(card)}}
            self.assertEqual(nullmoth.mark(rep), ["Fork card"])

    def test_where_the_config_is_read_from(self):
        cfg = os.path.join(self.tmp.name, "NullMoth", "1401")
        os.makedirs(cfg)
        with open(os.path.join(cfg, "driver-source.json"), "w") as fh:
            json.dump({"repo": "https://github.com/" + REPO, "public_key": KEY}, fh)
        src = ds.load({"XDG_CONFIG_HOME": self.tmp.name}, "linux")
        self.assertEqual((src.repo, src.official, src.builtin), (REPO, False, False))
        src = ds.load({"XDG_CONFIG_HOME": self.tmp.name, ds.ENV_REPO: "other/fork", ds.ENV_UNSIGNED: "1"}, "linux")
        self.assertEqual((src.repo, src.public_key, src.allow_unsigned), ("other/fork", None, True))
        with self.assertRaises(ds.SourceError):
            ds.load({ds.ENV_REPO: "other/fork"})  # no key, no allow_unsigned
        self.assertIs(ds.load({"XDG_CONFIG_HOME": self.tmp.name, ds.ENV_FILE: "official"}, "linux"), ds.DEFAULT)
        with self.assertRaisesRegex(ds.SourceError, "not a file"):
            ds.load({ds.ENV_FILE: os.path.join(self.tmp.name, "missing.json")})

    def test_a_broken_config_is_reported_not_ignored(self):
        p = os.path.join(self.tmp.name, "bad.json")
        with open(p, "w") as fh:
            fh.write("{not json")
        src, err = ds.load_safe({ds.ENV_FILE: p})
        self.assertIs(src, ds.DEFAULT)
        self.assertIn("bad.json", err)
        with mock.patch.object(nullmoth, "SOURCE_ERROR", err), self.assertRaisesRegex(RuntimeError, "config is wrong"):
            nullmoth.stage(os.path.join(self.tmp.name, "usb"), self.tmp.name, fetch=Fetch({}))
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "usb")))


class Stage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.usb, self.cache = os.path.join(self.tmp.name, "usb"), os.path.join(self.tmp.name, "cache")
        self.nm = os.path.join(self.usb, "NullMoth")

    def stage(self, src, blobs):
        out = io.StringIO()
        with redirect_stdout(out):
            dst = nullmoth.stage(self.usb, self.cache, fetch=Fetch(blobs), source=src)
        return dst, out.getvalue()

    def read(self, name):
        with open(os.path.join(self.nm, name)) as fh:
            return fh.read()

    def assertNothingStaged(self):
        self.assertFalse(os.path.exists(os.path.join(self.nm, NAME)))
        self.assertFalse(os.path.exists(os.path.join(self.cache, NAME)))

    def test_a_fork_package_signed_by_the_forks_key_is_placed_and_recorded(self):
        src = ds.from_dict({"repo": REPO, "public_key": KEY}, "/cfg/driver-source.json")
        dst, out = self.stage(src, release({NAME: DATA, NAME + ".sig": sig(DATA)}))
        with open(dst, "rb") as fh:
            self.assertEqual(fh.read(), DATA)
        self.assertIn(f"FORK github.com/{REPO}", out)
        readme = self.read("README.txt")
        self.assertIn(f"FORK of the NullMoth driver: github.com/{REPO}", readme)
        self.assertIn(SHA, readme)
        self.assertNotIn("NOT SIGNED", readme)
        m = json.loads(self.read("driver-source.json"))
        self.assertEqual((m["repo"], m["official"], m["signed"], m["package"]["sha256"], m["cards"]), (REPO, False, True, SHA, None))
        self.assertEqual(nullmoth.STAGED["verified"], f"fork key {KEY} (sig)")
        self.assertEqual(nullmoth.STAGED["source"]["repo"], REPO)

    def test_a_bound_signature_is_accepted(self):
        src = ds.from_dict({"repo": REPO, "public_key": KEY}, "t")
        self.stage(src, release({NAME: DATA, NAME + ".bsig": bsig(DATA)}))
        self.assertTrue(nullmoth.STAGED["verified"].endswith("(bsig)"))
        with self.assertRaisesRegex(RuntimeError, "signature"), redirect_stdout(io.StringIO()):  # another version: refused
            nullmoth.stage(os.path.join(self.tmp.name, "u2"), os.path.join(self.tmp.name, "c2"),
                           fetch=Fetch(release({NAME: DATA, NAME + ".bsig": bsig(DATA, "2.7.0")})), source=src)

    def test_a_signature_by_another_key_is_refused(self):
        src = ds.from_dict({"repo": REPO, "public_key": KEY}, "t")
        with self.assertRaisesRegex(RuntimeError, "signature"):
            self.stage(src, release({NAME: DATA, NAME + ".sig": sig(DATA, OTHER)}))
        self.assertNothingStaged()

    def test_a_fork_package_is_refused_against_nullmoths_key(self):
        # A NullMoth-config release from a fork's assets: nullmoth's repo prefix is required, and the key is NullMoth's.
        src = ds.from_dict({"repo": ds.OFFICIAL_REPO}, "t")
        api = f"https://api.github.com/repos/{ds.OFFICIAL_REPO}/releases/latest"
        with self.assertRaisesRegex(RuntimeError, "SHA256SUMS|served|no driver package"):
            self.stage(src, release({NAME: DATA, NAME + ".sig": sig(DATA)}, api=api))
        nm_dl = f"https://github.com/{ds.OFFICIAL_REPO}/releases/download/v2.8.0/"
        with self.assertRaisesRegex(RuntimeError, "signature"):  # signed, but not by NullMoth
            self.stage(src, release({NAME: DATA, NAME + ".sig": sig(DATA)}, api=api, base=nm_dl))
        self.assertNothingStaged()

    def test_an_unsigned_fork_package_needs_allow_unsigned_and_is_loudly_marked(self):
        src = ds.from_dict({"repo": REPO, "allow_unsigned": True}, "t")
        _, out = self.stage(src, release({NAME: DATA}))
        self.assertIn("WARNING", out)
        self.assertIn("NOT SIGNED", out)
        self.assertIn("THIS PACKAGE IS NOT SIGNED", self.read("README.txt"))
        m = json.loads(self.read("driver-source.json"))
        self.assertEqual((m["signed"], m["public_key"]), (False, None))
        self.assertEqual(nullmoth.STAGED["verified"], "UNSIGNED (allow_unsigned)")

    def test_a_fork_with_a_key_but_no_signature_is_refused(self):
        src = ds.from_dict({"repo": REPO, "public_key": KEY}, "t")
        with self.assertRaisesRegex(RuntimeError, "no signature"):
            self.stage(src, release({NAME: DATA}))
        self.assertNothingStaged()

    def test_no_sha256sums_or_an_unlisted_package_is_refused(self):
        src = ds.from_dict({"repo": REPO, "allow_unsigned": True}, "t")
        with self.assertRaisesRegex(RuntimeError, "SHA256SUMS"):
            self.stage(src, release({NAME: DATA}, sums=False))
        with self.assertRaisesRegex(RuntimeError, "SHA256SUMS"):
            self.stage(src, release({NAME: DATA}, sums={}))
        with self.assertRaisesRegex(RuntimeError, "SHA-256"):  # the download is not what SHA256SUMS.txt lists
            self.stage(src, release({NAME: DATA}, sums={NAME: "0" * 64}))
        self.assertNothingStaged()

    def test_assets_outside_the_forks_repository_are_refused(self):
        src = ds.from_dict({"repo": REPO, "allow_unsigned": True}, "t")
        with self.assertRaises(RuntimeError):
            self.stage(src, release({NAME: DATA}, base="https://evil.example/"))
        self.assertNothingStaged()

    def test_a_pinned_fork_package_must_match_its_release(self):
        pin = {"name": NAME, "url": DL + NAME, "sha256": SHA}
        src = ds.from_dict({"repo": REPO, "public_key": KEY, "package": pin}, "t")
        api = f"https://api.github.com/repos/{REPO}/releases/tags/v2.8.0"
        self.stage(src, release({NAME: DATA, NAME + ".sig": sig(DATA)}, api=api))
        self.assertEqual(nullmoth.STAGED["sha256"], SHA)
        src = ds.from_dict({"repo": REPO, "public_key": KEY, "package": dict(pin, sha256="1" * 64)}, "t")
        with self.assertRaisesRegex(RuntimeError, "does not list"), redirect_stdout(io.StringIO()):
            nullmoth.stage(os.path.join(self.tmp.name, "u3"), os.path.join(self.tmp.name, "c3"),
                           fetch=Fetch(release({NAME: DATA, NAME + ".sig": sig(DATA)}, api=api)), source=src)

    def test_the_card_table_rides_to_the_mac_app(self):
        src = ds.from_dict({"repo": REPO, "public_key": KEY, "cards": {"ids": {"FFF0": "Fork-only card"}}}, "t")
        table = ds.card_table(nullmoth.OFFICIAL_TABLE, src)
        with mock.patch.object(nullmoth, "TABLE", table):
            self.stage(src, release({NAME: DATA, NAME + ".sig": sig(DATA)}))
        self.assertEqual(json.loads(self.read("driver-source.json"))["cards"]["ids"]["FFF0"], "Fork-only card")

    def test_the_default_stays_nullmoths_pinned_package_and_clears_a_fork_manifest(self):
        os.makedirs(self.nm)
        with open(os.path.join(self.nm, "driver-source.json"), "w") as fh:
            fh.write("{}")
        os.makedirs(self.cache)
        body = b"pinned package"
        pkg = {"name": "pkg.tar.gz", "url": "unused", "sha256": hashlib.sha256(body).hexdigest()}
        with open(os.path.join(self.cache, "pkg.tar.gz"), "wb") as fh:
            fh.write(body)
        with mock.patch.dict(nullmoth.PACKAGE, pkg):
            dst, out = self.stage(ds.DEFAULT, {})
        self.assertEqual(out, "")
        self.assertEqual(sorted(os.listdir(self.nm)), ["README.txt", "pkg.tar.gz"])
        self.assertTrue(self.read("README.txt").startswith("NullMoth NVIDIA driver for macOS 15 and macOS 26.\n"))
        self.assertEqual(nullmoth.STAGED["verified"], "pinned SHA-256 in 1401")


class Update(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.rel = {"driver": (NAME, DL + NAME, SHA), "mac": None, "sigs": {}, "bsigs": {}}

    def test_update_from_a_fork_with_a_bound_signature(self):
        rel = dict(self.rel, bsigs={NAME + ".bsig": DL + NAME + ".bsig"})
        d = os.path.join(self.tmp.name, "a")
        nullmoth.update([d], rel, Fetch({DL + NAME: DATA, DL + NAME + ".bsig": bsig(DATA)}), sigkey=KEY, bound=True)
        self.assertEqual(os.listdir(d), [NAME])

    def test_update_from_a_fork_without_its_key_is_refused_unless_allowed(self):
        d = os.path.join(self.tmp.name, "b")
        with self.assertRaises(RuntimeError):
            nullmoth.update([d], self.rel, Fetch({DL + NAME: DATA}), sigkey=None, bound=True)
        self.assertEqual([f for f in os.listdir(d) if not f.endswith(".part")], [])
        lines = nullmoth.update([d], self.rel, Fetch({DL + NAME: DATA}), sigkey=None, allow_unsigned=True, bound=True)
        self.assertTrue(any("NOT SIGNED" in x for x in lines))
        self.assertEqual(os.listdir(d), [NAME])

    def test_latest_release_reads_the_forks_release_list(self):
        blobs = release({NAME: DATA, NAME + ".bsig": bsig(DATA)})
        api = f"https://api.github.com/repos/{REPO}/releases/latest"
        rel = nullmoth.latest_release(lambda u: json.loads(blobs[u]), lambda u: blobs[u].decode(), api)
        self.assertEqual(rel["driver"], (NAME, DL + NAME, SHA))
        self.assertIn(NAME + ".bsig", rel["bsigs"])


if __name__ == "__main__":
    unittest.main()

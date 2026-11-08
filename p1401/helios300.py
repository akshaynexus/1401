"""Acer Predator Helios 300 G3-571: a machine profile backed by the macos_helios300 release.

github.com/akshaynexus/macos_helios300 builds the drivers this laptop needs and nothing else has: PG106 (GTX 1060 Mobile,
GP106M 10de:1c20), PG106FB (its HDMI port), NMHDA (HDMI audio), QCARomePatchRAM (QCA6174 Bluetooth 04ca:3016),
Ath10kMac + AirportAth10k (QCA6174 Wi-Fi 168c:003e, Acer board 11ad:0807), two SSDTs, and the post-install pieces
(PG106Accel, the Metal plugin, NVK). Each release publishes manifest.json (macos_helios300 docs/MANIFEST.md): every
file's SHA-256 and URL, the OpenCore Kernel > Add order and Min/MaxKernel, ACPI adds, DeviceProperties, NVRAM, and what
must NOT be in the config. 1401 builds none of it: it downloads the release (the tag pinned with its manifest SHA-256 in
helios300_release.json, else the latest release), checks every file against the manifest and applies it literally. A
local release (--release-dir / --manifest) is used the same way, and --build-from-source only runs macos_helios300's
own tools/release.sh to make one (build_from_source).

The pass, from engine.build:
1. detect(): the scan shows this laptop (DMI product "Predator G3-571" and the GTX 1060 with Acer's subsystem 1025:118b).
2. prepare(): AppleALC with layout-id 29 ("dhinakg - Realtek ALC255 for Acer Predator G3-571" in AppleALC's ALC255
   list) instead of the VoodooHDA default - NMHDA pairs the HDMI audio through AppleALC/AppleHDA; the release is read.
3. keep_devices(): the engine disables the GTX 1060 (disable-gpu + an _OFF SSDT), its HDMI audio, the QCA6174 and the
   Bluetooth because it has no driver for them; with the release in hand they stay on, since PG106 drives the GPU.
   Without a release the engine's disabling stands (a dGPU with no driver is better off).
4. select_kexts(): the laptop basics the engine misses on a Linux scan (the Linux report names input devices by
   "Device ID", and the engine only reads "Device"): VoodooPS2Controller, VoodooI2C + VoodooI2CHID (SYNA7DB5),
   RealtekCardReader + RealtekCardReaderFriend for the RTL8411B, BlueToolFixup for QCARomePatchRAM.
5. apply(): after the engine wrote the EFI and before 1401's policy pass: the release's kexts, SSDTs, DeviceProperties,
   NVRAM and the forbidden list; the post-install pieces go to EFI/Helios300 with a README. Every change is a rule in
   the build result.

None of nullmoth.py's rules fire here: the GTX 1060 is not in the NullMoth driver's (Turing-or-newer) table, so no
NVRM/NVRMFB/NVAccel/NVRMAGDC, no nvfb/nvaccel boot-args, no NullMoth package on the stick, and no -wegnoegpu.
"""
import hashlib
import io
import json
import os
import plistlib
import re
import shutil
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
PIN_FILE = os.path.join(HERE, "helios300_release.json")
ENV_MANIFEST = "A1401_HELIOS_MANIFEST"   # a local dist/manifest.json (owner testing a release before it is published)
REPO = "akshaynexus/macos_helios300"
URL_PREFIX = f"https://github.com/{REPO}/releases/download/"
BOOT = "7C436110-AB2A-4BBB-A880-FE41995C9F82"
NAME = "Acer Predator Helios 300 G3-571"

# What detect() looks for in the scan. Mirrors machine.match of the release spec (macos_helios300
# tools/release/spec.json); the manifest's own match is checked again once it is loaded.
DMI_PRODUCT = "PREDATOR G3-571"
DGPU = ("10DE-1C20", "118B1025")          # GP106M, Acer subsystem 1025:118b (Hardware Sniffer: device then vendor)
IGPU = ("8086-591B", "118A1025")          # HD Graphics 630
WIFI = ("168C-003E", "080711AD")          # QCA6174, Lite-On 11ad:0807
BT = "04CA-3016"                          # QCA Rome Bluetooth (Lite-On)
CODEC = ("10EC-0255", "1025118A")         # Realtek ALC255 (codec subsystem: vendor then device)
HDMI_CODEC = "10DE-0084"                  # the GP106's HDMI codec (Sound section)
# AppleALC Resources/ALC255 layout 29: "dhinakg - Realtek ALC255 for Acer Predator G3-571" (OpCore-Simplify's
# codec_layouts.py lists it with that comment). The exact laptop, so no other layout is guessed.
LAYOUT_ID = "29"
# Kexts the release replaces or must never meet (also in the manifest's forbidden list): the NullMoth Turing+ stack,
# and Bluetooth/Wi-Fi drivers for other chips the engine may pick.
NEVER = ("NVRM.kext", "NVRMFB.kext", "NVAccel.kext", "NVRMAGDC.kext")
SMBIOS_NATIVE = "MacBookPro14,3"          # 15-inch 2017: i7-7700HQ + HD 630, the same CPU/iGPU; no CPUFriend data needed


class ReleaseError(RuntimeError):
    pass


def _devices(report, section):
    return [p for p in ((report or {}).get(section) or {}).values() if isinstance(p, dict)]


def _has(report, section, did, sub=None):
    for p in _devices(report, section):
        if str(p.get("Device ID") or "").upper() == did and (sub is None or str(p.get("Subsystem ID") or "").upper() == sub):
            return True
    return False


def detect(report):
    """{'name', 'parts': {...}} when the scan is this laptop, else None. The DMI product and the GTX 1060 with Acer's
    subsystem are required; the other parts are reported (a replaced Wi-Fi card just does not get the Wi-Fi kexts)."""
    board = str(((report or {}).get("Motherboard") or {}).get("Name") or "").upper()
    if DMI_PRODUCT not in board or not _has(report, "GPU", *DGPU):
        return None
    parts = {
        "dgpu": True,
        "igpu": _has(report, "GPU", *IGPU),
        "wifi": _has(report, "Network", *WIFI),
        "bt": _has(report, "Bluetooth", BT),
        "codec": _has(report, "Sound", *CODEC),
        "hdmi_codec": _has(report, "Sound", HDMI_CODEC),
    }
    return {"name": NAME, "parts": parts}


def _darwin(v):
    try:
        return tuple(int(x) for x in (str(v).split(".") + ["0", "0"])[:3])
    except ValueError:
        return (0, 0, 0)


def pin():
    try:
        with open(PIN_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _sha(blob):
    return hashlib.sha256(blob).hexdigest()


class Release:
    """A checked manifest plus where its files come from. files(name) -> bytes, each checked against the manifest."""

    def __init__(self, manifest, source, read):
        self.manifest, self.source, self._read = manifest, source, read

    @property
    def version(self):
        return self.manifest["release"]["version"]

    def artifact(self, name):
        return next((a for a in self.manifest["artifacts"] if a["name"] == name), None)

    def blob(self, art):
        data = self._read(art)
        if len(data) != art["size"] or _sha(data) != art["sha256"]:
            raise ReleaseError(f"{art['file']} does not match the SHA-256 in the macos_helios300 {self.version} manifest")
        return data

    def covers(self, macos_version):
        lo, hi = self.manifest["macos"]["min_kernel"], self.manifest["macos"]["max_kernel"]
        v = _darwin(macos_version)
        return (not lo or v >= _darwin(lo)) and (not hi or v <= _darwin(hi))


def check_manifest(m, local=False):
    """Structural checks on a manifest (the full JSON Schema lives in macos_helios300). Raises ReleaseError."""
    def need(cond, what):
        if not cond:
            raise ReleaseError(f"the macos_helios300 manifest is not usable: {what}")
    need(isinstance(m, dict) and m.get("schema") == 1, "schema is not 1")
    need(m.get("repo") == REPO, f"repo is {m.get('repo')!r}, not {REPO}")
    rel = m.get("release") or {}
    need(re.fullmatch(r"\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?", str(rel.get("version") or "")), "no release version")
    need(local or not rel.get("dry_run"), "it is a dry-run manifest (release.sh --dry-run); only a local test may use one")
    need(isinstance(m.get("artifacts"), list) and m["artifacts"], "no artifacts")
    for a in m["artifacts"]:
        need(isinstance(a, dict) and re.fullmatch(r"[0-9a-f]{64}", str(a.get("sha256") or "")), f"artifact without sha256: {a!r:.80}")
        need(re.fullmatch(r"[A-Za-z0-9_.+-]+", str(a.get("file") or "")), f"bad file name {a.get('file')!r}")
        need(str(a.get("url") or "").startswith(URL_PREFIX + "v" + rel["version"] + "/"),
             f"{a.get('file')} is not served from {URL_PREFIX}v{rel['version']}/")
        need(isinstance(a.get("size"), int) and a["size"] > 0, f"{a.get('file')} has no size")
        need(a.get("install") in ("opencore", "library-extensions", "gpu-bundles", "applications", "stick-only"),
             f"{a.get('name')}: unknown install {a.get('install')!r}")
        if a.get("install") == "opencore" and a.get("kind") == "kext":
            oc = a.get("opencore") or {}
            need(oc.get("BundlePath") == a["name"] and oc.get("ExecutablePath") and oc.get("PlistPath") == "Contents/Info.plist",
                 f"{a['name']}: incomplete Kernel > Add entry")
        if a.get("kind") == "ssdt":
            need((a.get("acpi") or {}).get("Path") == a["name"], f"{a['name']}: incomplete ACPI > Add entry")
    cfg = m.get("config") or {}
    for k in ("nvram_add", "nvram_delete", "device_properties", "forbidden"):
        need(isinstance(cfg.get(k), dict), f"config.{k} missing")
    need(isinstance(m.get("macos"), dict), "no macos range")
    return m


API_RELEASES = f"https://api.github.com/repos/{REPO}/releases?per_page=30"
SOURCE_REPO = f"https://github.com/{REPO}.git"
TOOLCHAIN_HELP = (f"https://github.com/{REPO}/blob/main/docs/MANIFEST.md#how-a-release-is-built "
                  "(toolchain: macOS 26.5 SDK, Apple ld64 from cctools-port, ld64.lld, xnu-12377 export lists, rustup "
                  "x86_64-apple-darwin, iasl, rcodesign; defaults in tools/cross-build-kext.sh)")


def _vkey(tag):
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(-.*)?", str(tag or ""))
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4) is None) if m else (-1, -1, -1, False)


def resolve_latest(fetch):
    """The newest published macos_helios300 release (pre-releases count: every release is one until a Mac
    verification build) that carries a manifest.json, or None when there is none yet."""
    rels = json.loads(fetch(API_RELEASES).decode("utf-8"))
    for r in rels if isinstance(rels, list) else []:
        if r.get("draft"):
            continue
        if any(a.get("name") == "manifest.json" for a in r.get("assets") or []):
            return r.get("tag_name")
    return None


def _cached_tags(cache_dir):
    try:
        tags = [t for t in os.listdir(cache_dir) if os.path.isfile(os.path.join(cache_dir, t, "manifest.json"))]
    except OSError:
        return []
    return sorted(tags, key=_vkey, reverse=True)


def _local_release(path):
    path = os.path.abspath(path)
    if os.path.isdir(path):
        path = os.path.join(path, "manifest.json")
    base = os.path.dirname(path)
    try:
        with open(path, "rb") as fh:
            raw_local = fh.read()
        m = check_manifest(json.loads(raw_local.decode("utf-8")), local=True)
    except (OSError, ValueError) as e:
        raise ReleaseError(f"the local macos_helios300 manifest {path} could not be read: {e}") from e

    def read_local(art):
        try:
            with open(os.path.join(base, art["file"]), "rb") as fh:
                return fh.read()
        except OSError as e:
            raise ReleaseError(f"{art['file']} is not next to {path}") from e
    return Release(m, {"kind": "local", "path": path, "pinned": False, "dry_run": bool(m["release"].get("dry_run")),
                       "manifest_sha256": _sha(raw_local)}, read_local)


def load_release(fetch=None, cache_dir=None, env=None, release=None, manifest=None):
    """(Release or None, why-not). Nothing is built here: a release is downloaded (or read from the cache or a local
    directory), its manifest checked, and every file checked against the manifest when it is used.

    manifest: a local manifest.json or a release directory (--manifest / --release-dir / --build-from-source); it wins
    and is reported as unpinned. $A1401_HELIOS_MANIFEST does the same.
    release: a tag ("v0.1.0") or "latest"; default: the tag pinned in helios300_release.json, else the latest release.
    fetch(url) -> bytes, or None offline: then only the cache (helios300-release/<tag>/) is used."""
    env = os.environ if env is None else env
    local = manifest or env.get(ENV_MANIFEST)
    if local:
        return _local_release(local), None
    p = pin()
    tag = release or p.get("tag") or "latest"
    want = p.get("manifest_sha256") if p.get("tag") and tag == p.get("tag") else None
    if tag == "latest":
        found, why = None, "no macos_helios300 release has been published yet"
        if fetch is not None:
            try:
                found = resolve_latest(fetch)
            except Exception as e:  # noqa: BLE001 - offline or GitHub refused: the cache may still have one
                why = f"the macos_helios300 releases could not be listed ({type(e).__name__}: {e})"
        else:
            why = "the network is not used (planning only) and no release is cached"
        cached = _cached_tags(cache_dir) if cache_dir else []
        tag = found or (cached[0] if cached else None)
        if not tag:
            return None, why
    if not re.fullmatch(r"v\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?", tag):
        raise ReleaseError(f"{tag!r} is not a macos_helios300 release tag (vX.Y.Z)")
    url = f"{URL_PREFIX}{tag}/manifest.json"
    cdir = os.path.join(cache_dir, tag) if cache_dir else None
    raw = None
    if cdir and os.path.isfile(os.path.join(cdir, "manifest.json")):
        with open(os.path.join(cdir, "manifest.json"), "rb") as fh:
            raw = fh.read()
        if want and _sha(raw) != want:
            raw = None
    if raw is None:
        if fetch is None:
            return None, f"macos_helios300 {tag} is not in the cache and the network is not used (planning only)"
        raw = fetch(url)
        if want and _sha(raw) != want:
            raise ReleaseError(f"{url} does not match the SHA-256 this 1401 pins for macos_helios300 {tag}")
        if cdir:
            os.makedirs(cdir, exist_ok=True)
            with open(os.path.join(cdir, "manifest.json.part"), "wb") as fh:
                fh.write(raw)
            os.replace(os.path.join(cdir, "manifest.json.part"), os.path.join(cdir, "manifest.json"))
    m = check_manifest(json.loads(raw.decode("utf-8")))
    if "v" + m["release"]["version"] != tag:
        raise ReleaseError(f"the {tag} manifest says v{m['release']['version']}")

    def read_release(art):
        path = os.path.join(cdir, art["file"]) if cdir else None
        if path and os.path.isfile(path):
            with open(path, "rb") as fh:
                data = fh.read()
            if _sha(data) == art["sha256"]:
                return data
        if fetch is None:
            raise ReleaseError(f"{art['file']} is not in the macos_helios300 cache and the network is not used")
        data = fetch(art["url"])
        if path and _sha(data) == art["sha256"]:
            with open(path + ".part", "wb") as fh:
                fh.write(data)
            os.replace(path + ".part", path)
        return data
    return Release(m, {"kind": "release", "tag": tag, "url": url, "pinned": bool(want), "manifest_sha256": _sha(raw)},
                   read_release), None


def build_from_source(ref, work_dir, run=None, env=None):
    """Optional: build the release locally with macos_helios300's OWN tools/release.sh and return its
    dist/manifest.json, which then goes through exactly the same checks and assembly as a downloaded release.
    1401 builds nothing itself: it clones (or reuses) the repo at ref, initialises its submodules and runs release.sh.
    A missing cross toolchain (release.sh exit 77) stops with a pointer to macos_helios300's build docs."""
    import subprocess  # noqa: PLC0415
    run = run or subprocess.run
    env = dict(os.environ if env is None else env)
    ref = ref or "main"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}", ref) or ".." in ref:
        raise ReleaseError(f"{ref!r} is not a git tag, branch or commit")
    src = os.path.join(work_dir, "macos_helios300")
    log = os.path.join(work_dir, "release.log")
    os.makedirs(work_dir, exist_ok=True)

    def git(*args, cwd=src):
        r = run(["git", *args], cwd=cwd, capture_output=True, text=True)
        if r.returncode != 0:
            raise ReleaseError(f"git {' '.join(args)} failed: {(r.stderr or r.stdout or '').strip()[-400:]}")
        return (r.stdout or "").strip()
    if not os.path.isdir(os.path.join(src, ".git")):
        git("clone", "--quiet", SOURCE_REPO, src, cwd=work_dir)
    git("fetch", "--quiet", "--tags", "origin")
    target = ref                                   # a tag or a commit
    if not re.fullmatch(r"[0-9a-f]{7,40}", ref) and git("ls-remote", "--heads", "origin", ref):
        target = f"origin/{ref}"                   # a branch: its current tip on GitHub
    git("checkout", "--quiet", "--detach", target)
    git("submodule", "update", "--init", "--quiet")
    commit = git("rev-parse", "HEAD")
    m = re.fullmatch(r"v(\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?)", ref)
    version = m.group(1) if m else f"0.0.0-src.{commit[:12]}"
    dist = os.path.join(work_dir, "dist")
    cmd = ["bash", os.path.join(src, "tools", "release.sh"), "--version", version, "--dist", dist]
    if env.get("HELIOS_IAF2"):
        cmd += ["--iaf2", env["HELIOS_IAF2"]]
    print(f"macos_helios300 {ref} ({commit[:12]}): running its tools/release.sh (this takes a while; log: {log})", flush=True)
    with open(log, "w", encoding="utf-8") as fh:
        r = run(cmd, cwd=src, stdout=fh, stderr=subprocess.STDOUT, env=env)
    if r.returncode == 77:
        raise ReleaseError("building macos_helios300 from source needs its Linux cross toolchain, and part of it is "
                           f"missing (release.sh said SKIP; {log}). Set it up as {TOOLCHAIN_HELP}, or build without "
                           "--build-from-source to use the published release.")
    if r.returncode != 0:
        try:
            with open(log, encoding="utf-8", errors="replace") as fh:
                tail = fh.read()[-1500:]
        except OSError:
            tail = ""
        raise ReleaseError(f"macos_helios300's tools/release.sh failed (exit {r.returncode}); see {log}:\n{tail}")
    manifest = os.path.join(dist, "manifest.json")
    if not os.path.isfile(manifest):
        raise ReleaseError(f"tools/release.sh finished but wrote no {manifest}")
    return manifest


# ---- engine hooks ---------------------------------------------------------------------------------------------------

def prepare(policy, hit, notices):
    """Before the engine runs: AppleALC + layout 29 (policy.tahoe_audio, policy.prefer['CodecLayout'])."""
    import dataclasses  # noqa: PLC0415
    prefer = dict(policy.prefer)
    if hit["parts"].get("codec"):
        prefer["CodecLayout"] = LAYOUT_ID
    notices.append(f"{NAME}: macos_helios300 machine profile (GTX 1060 Mobile, QCA6174 Wi-Fi/Bluetooth, ALC255 layout {LAYOUT_ID}).")
    return dataclasses.replace(policy, tahoe_audio="applealc", prefer=prefer)


def _covered(release, macos_version, device_id):
    """True when the release has an OpenCore artifact for this device and the macOS version is in its range."""
    if release is None or not release.covers(macos_version):
        return False
    vid, did = device_id.lower().split("-")
    for a in release.manifest["artifacts"]:
        if a.get("install") != "opencore":
            continue
        for h in a.get("hardware") or []:
            if h.get("vendor") == vid and h.get("device") == did:
                return True
    return False


def keep_devices(disabled, release, macos_version, notices):
    """The engine's disabled devices minus those this release drives (the GPU, its HDMI audio, the Wi-Fi card, the
    Bluetooth). Returns (disabled, kept names)."""
    keep = {}
    for k, v in (disabled or {}).items():
        did = str((v or {}).get("Device ID") or "").upper() if isinstance(v, dict) else str(v).upper()
        if did == HDMI_CODEC:
            covered = _covered(release, macos_version, "10de-10f1") or _covered(release, macos_version, DGPU[0])
        else:
            covered = did in (DGPU[0], WIFI[0], BT) and _covered(release, macos_version, did)
        if covered:
            keep[k] = v
    if keep:
        notices.append(f"{NAME}: kept on for the macos_helios300 drivers: " + ", ".join(sorted(keep)))
    return {k: v for k, v in (disabled or {}).items() if k not in keep}, sorted(keep)


def select_kexts(km, macos_version, release, notices):
    """The laptop's kexts the engine does not pick from a Linux scan, through the engine's own kext list."""
    from Scripts.datasets import kext_data  # noqa: PLC0415 - importable once the engine is loaded
    idx = kext_data.kext_index_by_name
    added, failed = [], []
    want = ["VoodooPS2Controller", "VoodooI2C", "VoodooI2CHID", "RealtekCardReader", "RealtekCardReaderFriend"]
    if release is not None and release.covers(macos_version) and release.artifact("QCARomePatchRAM.kext"):
        want.append("BlueToolFixup")
    for name in want:
        i = idx.get(name)
        if i is None:
            failed.append(name)
            continue
        if km.kexts[i].checked:
            continue
        if km.check_kext(i, macos_version) is False or not km.kexts[i].checked:
            failed.append(name)
        else:
            added.append(name)
    j = idx.get("Sinetek-rtsx")
    if j is not None and km.kexts[j].checked and "RealtekCardReader" not in failed:
        km.kexts[j].checked = False   # RealtekCardReader drives the RTL8411B (10ec:5287); one card-reader driver only
        added.append("-Sinetek-rtsx")
    if added:
        notices.append(f"{NAME}: laptop kexts " + ", ".join(added))
    if failed:
        notices.append(f"{NAME}: could not select {', '.join(failed)} for this macOS version")
    return added, failed


# ---- the EFI pass ---------------------------------------------------------------------------------------------------

def _plist_value(v):
    if "data" in v:
        return bytes.fromhex(v["data"])
    if "string" in v:
        return v["string"]
    if "integer" in v:
        return int(v["integer"])
    if "boolean" in v:
        return bool(v["boolean"])
    raise ReleaseError(f"unknown value type {v!r}")


def _extract_bundle(release, art, dest_dir):
    """Unzips <name>/... of a verified zip into dest_dir and checks every file against contents_sha256."""
    data = release.blob(art)
    name = art["name"]
    want = art.get("contents_sha256") or {}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        members = [i for i in z.infolist() if not i.is_dir()]
        for i in z.infolist():
            n = i.filename
            if n.startswith("/") or ".." in n.split("/") or not (n == name + "/" or n.startswith(name + "/")):
                raise ReleaseError(f"{art['file']}: unexpected entry {n!r}")
        got = {i.filename[len(name) + 1:]: i for i in members}
        if want and set(got) != set(want):
            raise ReleaseError(f"{art['file']}: its files differ from the manifest's list")
        target = os.path.join(dest_dir, name)
        shutil.rmtree(target, ignore_errors=True)
        for rel, info in sorted(got.items()):
            blob = z.read(info)
            if want and _sha(blob) != want[rel]:
                raise ReleaseError(f"{name}/{rel} does not match the manifest")
            path = os.path.join(target, *rel.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(blob)
            if (info.external_attr >> 16) & 0o111:
                os.chmod(path, 0o755)
    return target


def _entry_like(existing, values):
    """A config entry with the keys this OpenCore's own entries carry, filled from values."""
    keys = list(existing[0].keys()) if existing and isinstance(existing[0], dict) else list(values.keys())
    out = {k: values[k] for k in values if k != "order"}
    for k in keys:
        if k not in out:
            out[k] = existing[0][k] if not isinstance(existing[0][k], (bytes, str)) else type(existing[0][k])()
    return out


POST_README = """macos_helios300 {version} post-install kit for the {name}
=====================================================================

These were built by macos_helios300 tools/release.sh and checked by 1401 against the release manifest
(SHA-256 of every file). OpenCore cannot inject them; install them after macOS 26 is running.

{steps}

After installing macOS 26 (also listed in 1401's build result):
- The HD 630 needs no patch: macOS 26.7.2 still ships the Kaby Lake drivers (Metal 3 on the first boot).
- Audio: macOS 26 removed AppleHDA (AppleALC and NMHDA need it). Do NOT let OpenCore Legacy Patcher apply its
  "Nvidia Web Drivers" set: they would take the GTX 1060 from PG106. OCLP refuses to patch while it lists them;
  macos_helios300 docs/HARDWARE.md has the audio-only route. SIP is already lowered.
- USB: SSDT-HS07-UPC marks the Bluetooth port internal; map the other ports with USBToolBox for USB 3 speeds.

Sources, licences and status of every file: manifest.json next to this README, and
https://github.com/akshaynexus/macos_helios300/blob/main/docs/MANIFEST.md
"""


def _add_uefi_shell(cfg, oc, change, ock_cache=None):
    """A 'UEFI Shell' picker entry: a terminal before macOS that can read and edit the EFI (config.plist) when a driver
    build keeps macOS and Recovery from starting (first macOS boots, 2026-11-08). OpenShell.efi comes from the same
    OpenCorePkg the engine built this EFI from, so it matches the OpenCore on the stick."""
    tools = cfg.setdefault("Misc", {}).setdefault("Tools", [])
    if any(str(t.get("Path", "")).lower() == "openshell.efi" for t in tools):
        return
    dst = os.path.join(oc, "Tools", "OpenShell.efi")
    if not os.path.isfile(dst):
        if ock_cache is None:
            from .engine import OCK_CACHE  # noqa: PLC0415
            ock_cache = OCK_CACHE
        srcf = os.path.join(ock_cache, "OpenCorePkg", "EFI", "OC", "Tools", "OpenShell.efi")
        if not os.path.isfile(srcf):
            change("helios-uefi-shell", "", "not added: OpenShell.efi is not in the OpenCorePkg download",
                   "the picker keeps Recovery and Reset NVRAM")
            return
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(srcf, dst)
    tools.append({"Arguments": "", "Auxiliary": False, "Comment": "UEFI Shell: terminal before macOS", "Enabled": True,
                  "Flavour": "OpenShell:UEFIShell:Shell", "FullNvramAccess": False, "Name": "UEFI Shell",
                  "Path": "OpenShell.efi", "RealPath": False, "TextMode": False})
    change("helios-uefi-shell", "", "Misc→Tools: UEFI Shell (OpenShell.efi)",
           "a terminal in the boot menu to edit config.plist when macOS and Recovery cannot start")


def apply(config_path, release, macos_version, hardware, change):
    """Puts the release into the built EFI. change(rule, before, after, why) records each step. Returns a summary."""
    oc = os.path.dirname(config_path)
    efi = os.path.dirname(oc)
    with open(config_path, "rb") as fh:
        cfg = plistlib.load(fh)
    m = release.manifest
    src = release.source
    how = (f"pinned manifest {src['manifest_sha256'][:16]}" if src["pinned"] else
           f"release {src['tag']}, manifest {src['manifest_sha256'][:16]} not pinned in this 1401" if src["kind"] == "release" else
           f"LOCAL, UNPINNED: {src['path']}")
    change("helios-release", "", f"macos_helios300 {release.version} ({how}" + (", DRY RUN" if src.get("dry_run") else "") + ")",
           "the drivers for this laptop's GTX 1060, Wi-Fi, Bluetooth and HDMI audio come from its own release, checked file by file")
    if not release.covers(macos_version):
        change("helios-macos-range", "", f"not added: the release covers Darwin {m['macos']['min_kernel']}-{m['macos']['max_kernel']}",
               f"this build is for Darwin {macos_version}; build for macOS 26 to get the GTX 1060, Wi-Fi and Bluetooth drivers")
        return {"added": [], "post_install": []}
    hw = hardware or {}
    present = {"pci": set(), "usb": set()}
    for sect in ("GPU", "Network", "Sound", "USB Controllers", "Storage Controllers", "SD Controller", "System Devices"):
        for p in _devices(hw, sect):
            did = str(p.get("Device ID") or "").lower()
            if re.fullmatch(r"[0-9a-f]{4}-[0-9a-f]{4}", did):
                present["pci"].add(did.replace("-", ":"))
    for p in _devices(hw, "Bluetooth"):
        present["usb"].add(str(p.get("Device ID") or "").lower().replace("-", ":"))

    def fits(art):
        hws = art.get("hardware") or []
        return not hws or any(f"{h['vendor']}:{h['device']}" in present.get(h.get("bus", "pci"), set()) for h in hws)

    kadd = cfg.setdefault("Kernel", {}).setdefault("Add", [])
    enabled = {str(k.get("BundlePath", "")).split("/")[0] for k in kadd if isinstance(k, dict) and k.get("Enabled")}
    added, skipped = [], []
    oc_arts = sorted((a for a in m["artifacts"] if a.get("install") == "opencore"),
                     key=lambda a: (a.get("opencore") or a.get("acpi") or {}).get("order", 0))
    for a in oc_arts:
        if not fits(a):
            skipped.append(f"{a['name']} (its hardware is not in this scan)")
            continue
        if a["kind"] == "kext":
            missing = [r for r in a.get("requires") or [] if r not in enabled]
            if missing:
                skipped.append(f"{a['name']} (needs {', '.join(missing)}, which this build does not load)")
                continue
            for c in a.get("conflicts") or []:
                for k in kadd:
                    if isinstance(k, dict) and str(k.get("BundlePath", "")).split("/")[0] == c and k.get("Enabled"):
                        k["Enabled"] = False
                        change("helios-kext-conflict", c, "off", f"{a['name']} drives the same device")
            _extract_bundle(release, a, os.path.join(oc, "Kexts"))
            entry = dict(a["opencore"])
            entry.pop("order", None)
            exe = os.path.join(oc, "Kexts", a["name"], *entry["ExecutablePath"].split("/"))
            if not os.path.isfile(exe):
                raise ReleaseError(f"{a['name']}: {entry['ExecutablePath']} is not in the bundle")
            kadd[:] = [k for k in kadd if not (isinstance(k, dict) and k.get("BundlePath") == a["name"])]
            kadd.append(_entry_like(kadd, entry))
            enabled.add(a["name"])
            added.append(a["name"])
        elif a["kind"] == "ssdt":
            blob = release.blob(a)
            os.makedirs(os.path.join(oc, "ACPI"), exist_ok=True)
            with open(os.path.join(oc, "ACPI", a["name"]), "wb") as fh:
                fh.write(blob)
            aadd = cfg.setdefault("ACPI", {}).setdefault("Add", [])
            aadd[:] = [x for x in aadd if not (isinstance(x, dict) and x.get("Path") == a["name"])]
            entry = dict(a["acpi"])
            entry.pop("order", None)
            aadd.append(_entry_like(aadd, entry))
            added.append(a["name"])
    if added:
        change("helios-kexts", "", ", ".join(added),
               "macos_helios300 artifacts in the manifest's Kernel > Add / ACPI > Add order, each checked against its SHA-256")
    if skipped:
        change("helios-skipped", "", "; ".join(skipped), "an artifact is added only for hardware the scan shows and with what it needs")

    c = m["config"]
    dp = cfg.setdefault("DeviceProperties", {}).setdefault("Add", {})
    for path, props in c.get("device_properties", {}).items():
        d = dp.setdefault(path, {})
        for k, v in props.items():
            val = _plist_value(v)
            if d.get(k) != val:
                change("helios-device-properties", f"{path} {k}={d.get(k)!r}", f"{k}={val!r}",
                       "the GP106 and its HDMI audio share hda-gfx onboard-2 (WhateverGreen gives the HD 630 onboard-1)")
                d[k] = val
    # Boot hotkeys in the OpenCore picker: Shift+Enter starts macOS safe mode (-x), Cmd+V verbose. The Helios kexts
    # (PG106, PG106FB, Ath10kMac, AirportAth10k, QCARomePatchRAM) stand down under -x, so a bad driver build can be
    # bypassed from the picker without Recovery or the USB stick (first macOS boots, 2026-11-08).
    boot = cfg.setdefault("Misc", {}).setdefault("Boot", {})
    if boot.get("PollAppleHotKeys") is not True:
        change("helios-hotkeys", f"PollAppleHotKeys {boot.get('PollAppleHotKeys')}", "PollAppleHotKeys True",
               "Shift+Enter = safe mode (the Helios kexts stand down), Cmd+V = verbose, from the OpenCore picker")
        boot["PollAppleHotKeys"] = True
    inp = cfg.setdefault("UEFI", {}).setdefault("Input", {})
    if inp.get("KeySupport") is not True:
        change("helios-hotkeys", f"KeySupport {inp.get('KeySupport')}", "KeySupport True", "the picker hotkeys need it")
        inp["KeySupport"] = True
    _add_uefi_shell(cfg, oc, change)
    nv_add = cfg.setdefault("NVRAM", {}).setdefault("Add", {})
    nv_del = cfg["NVRAM"].setdefault("Delete", {})
    for guid, keys in c.get("nvram_add", {}).items():
        dst = nv_add.setdefault(guid, {})
        for k, v in keys.items():
            val = _plist_value(v)
            if dst.get(k) != val:
                change("helios-nvram", f"{k}={dst.get(k)!r}", f"{k}={val!r}", "BrcmPatchRAM 2.7+ (BlueToolFixup) needs these on every macOS")
                dst[k] = val
    for guid, keys in c.get("nvram_delete", {}).items():
        lst = nv_del.setdefault(guid, [])
        for k in keys:
            if k not in lst:
                lst.append(k)
    nv = nv_add.setdefault(BOOT, {})
    args = (nv.get("boot-args") or "").split()
    for a in c.get("boot_args") or []:
        if a not in args:
            args.append(a)
            change("helios-boot-args", "", a, "required by the macos_helios300 drivers")

    forb = c.get("forbidden") or {}
    gone = [x for x in args if x in (forb.get("boot_args") or [])]
    if gone:
        args = [x for x in args if x not in gone]
        change("helios-forbidden", " ".join(gone), "", "PG106 drives the GTX 1060; a boot-arg that hides it would stop the driver")
    if args != (nv.get("boot-args") or "").split():
        nv["boot-args"] = " ".join(args)
    for k in kadd:
        if isinstance(k, dict) and k.get("Enabled") and str(k.get("BundlePath", "")).split("/")[0] in set(forb.get("kexts") or []) | set(NEVER):
            k["Enabled"] = False
            change("helios-forbidden", k["BundlePath"], "off", "not for this machine (macos_helios300 manifest forbidden list)")
    for path, keys in (forb.get("device_properties") or {}).items():
        d = dp.get(path) or {}
        for key in keys:
            if key in d:
                del d[key]
                change("helios-forbidden", f"{path} {key}", "", "PG106 drives the GTX 1060; it must not be disabled")
    for x in cfg.get("ACPI", {}).get("Add", []):
        if isinstance(x, dict) and x.get("Enabled") and x.get("Path") in (forb.get("acpi") or []):
            x["Enabled"] = False
            change("helios-forbidden", x["Path"], "off", "this SSDT powers the GTX 1060 off; PG106 drives it")

    # Post-install pieces (OpenCore cannot inject them) + licences, on the stick under EFI/Helios300.
    kit = os.path.join(efi, "Helios300")
    shutil.rmtree(kit, ignore_errors=True)
    post = []
    for a in m["artifacts"]:
        if a.get("install") == "opencore" or not fits(a):
            continue
        os.makedirs(kit, exist_ok=True)
        with open(os.path.join(kit, a["file"]), "wb") as fh:
            fh.write(release.blob(a))
        post.append(a["file"])
    if post:
        with open(os.path.join(kit, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(m, fh, indent=1)
        steps = "\n".join(f"- {s['what']} ({', '.join(s['artifacts'])})" for s in m.get("post_install") or [])
        with open(os.path.join(kit, "README.txt"), "w", encoding="utf-8") as fh:
            fh.write(POST_README.format(version=release.version, name=NAME, steps=steps))
        change("helios-post-install", "", "EFI/Helios300: " + ", ".join(post),
               "PG106Accel, the Metal plugin and NVK load from macOS itself; they go on the stick for after the install")

    tmp = config_path + ".tmp"
    with open(tmp, "wb") as fh:
        plistlib.dump(cfg, fh)
    os.replace(tmp, config_path)
    return {"added": added, "post_install": post, "skipped": skipped}


def notes(res, release, why_not, kept):
    """Rules for the build result that do not change the config (recorded by engine.build through change())."""
    out = []
    if release is None:
        out.append(("helios-release-pending", "", "GTX 1060, Wi-Fi, Bluetooth and HDMI-audio drivers NOT added: " + (why_not or "no release"),
                    "this build runs the laptop on the HD 630 with the engine's own kexts; build again once macos_helios300 is released"))
    if kept:
        out.append(("helios-keep-devices", "disabled by the engine: " + ", ".join(kept), "kept on",
                    "macos_helios300 has drivers for them, so they are not disabled or powered off"))
    if res.smbios and res.smbios != SMBIOS_NATIVE:
        out.append(("helios-cpufriend", "", f"SMBIOS {res.smbios}: add CPUFriend + CPUFriendDataProvider made from {SMBIOS_NATIVE}",
                    "the i7-7700HQ's power data is MacBookPro14,3's; another model's data runs it at the wrong frequencies"))
    if _darwin(res.macos_version) >= (25, 0, 0):
        out.append(("helios-oclp-post-install", "", "after installing: AppleHDA for audio (not OCLP's Nvidia Web Drivers)",
                    "macOS 26 removed AppleHDA (AppleALC/NMHDA need it); the HD 630 is native on 26.7.2 (Kaby Lake drivers, Metal 3)"))
    if release is not None:
        out.append(("helios-usb-map", "", "UTBDefault + SSDT-HS07-UPC (Bluetooth port internal)",
                    "the scan has no physical port map; run USBToolBox after the install for USB 3 on every port"))
    return out

"""Acer Predator Helios 300 G3-571: a machine profile backed by the macos_helios300 release.

github.com/akshaynexus/macos_helios300 builds the drivers this laptop needs and nothing else has: PG106 (GTX 1060 Mobile,
GP106M 10de:1c20), PG106FB (its HDMI port), NMHDA (HDMI audio), QCARomePatchRAM (QCA6174 Bluetooth 04ca:3016),
Ath10kMac + AirportAth10k (QCA6174 Wi-Fi 168c:003e, Acer board 11ad:0807), two SSDTs, and the post-install pieces
(PG106Accel, the Metal plugin, NVK). Each release publishes manifest.json (macos_helios300 docs/MANIFEST.md): every
file's SHA-256 and URL, the OpenCore Kernel > Add order and Min/MaxKernel, ACPI adds, DeviceProperties, NVRAM, and what
must NOT be in the config. 1401 pins that manifest by its own SHA-256 (helios300_release.json) and applies it literally.

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


def load_release(fetch=None, cache_dir=None, env=None):
    """(Release or None, why-not). fetch(url) -> bytes, or None offline (then only the cache is used).
    A local manifest named by $A1401_HELIOS_MANIFEST wins and is reported as unpinned."""
    env = os.environ if env is None else env
    local = env.get(ENV_MANIFEST)
    if local:
        base = os.path.dirname(os.path.abspath(local))
        try:
            with open(local, "rb") as fh:
                raw_local = fh.read()
            m = check_manifest(json.loads(raw_local.decode("utf-8")), local=True)
        except (OSError, ValueError) as e:
            raise ReleaseError(f"{ENV_MANIFEST}={local} could not be read: {e}") from e

        def read_local(art):
            with open(os.path.join(base, art["file"]), "rb") as fh:
                return fh.read()
        return Release(m, {"kind": "local", "path": os.path.abspath(local), "pinned": False,
                           "dry_run": bool(m["release"].get("dry_run")),
                           "manifest_sha256": _sha(raw_local)}, read_local), None
    p = pin()
    tag, want = p.get("tag"), p.get("manifest_sha256")
    if not tag or not want:
        return None, "no macos_helios300 release is pinned in this 1401 yet (helios300_release.json)"
    url = f"{URL_PREFIX}{tag}/manifest.json"
    cdir = os.path.join(cache_dir, tag) if cache_dir else None
    raw = None
    if cdir and os.path.isfile(os.path.join(cdir, "manifest.json")):
        with open(os.path.join(cdir, "manifest.json"), "rb") as fh:
            raw = fh.read()
        if _sha(raw) != want:
            raw = None
    if raw is None:
        if fetch is None:
            return None, f"macos_helios300 {tag} is not in the cache and the network is not used (planning only)"
        raw = fetch(url)
        if _sha(raw) != want:
            raise ReleaseError(f"{url} does not match the SHA-256 this 1401 pins for macos_helios300 {tag}")
        if cdir:
            os.makedirs(cdir, exist_ok=True)
            with open(os.path.join(cdir, "manifest.json.part"), "wb") as fh:
                fh.write(raw)
            os.replace(os.path.join(cdir, "manifest.json.part"), os.path.join(cdir, "manifest.json"))
    m = check_manifest(json.loads(raw.decode("utf-8")))
    if "v" + m["release"]["version"] != tag:
        raise ReleaseError(f"the pinned manifest is for v{m['release']['version']}, not {tag}")

    def read_pinned(art):
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
    return Release(m, {"kind": "pinned", "tag": tag, "url": url, "pinned": True, "manifest_sha256": want}, read_pinned), None


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
- OpenCore Legacy Patcher root patches: AppleHDA (macOS 26 removed it; AppleALC and NMHDA need it) and the
  Kaby Lake graphics patch for the HD 630 (macos_helios300 docs/HARDWARE.md). SIP is already lowered for them.
- USB: SSDT-HS07-UPC marks the Bluetooth port internal; map the other ports with USBToolBox for USB 3 speeds.

Sources, licences and status of every file: manifest.json next to this README, and
https://github.com/akshaynexus/macos_helios300/blob/main/docs/MANIFEST.md
"""


def apply(config_path, release, macos_version, hardware, change):
    """Puts the release into the built EFI. change(rule, before, after, why) records each step. Returns a summary."""
    oc = os.path.dirname(config_path)
    efi = os.path.dirname(oc)
    with open(config_path, "rb") as fh:
        cfg = plistlib.load(fh)
    m = release.manifest
    src = release.source
    change("helios-release", "", f"macos_helios300 {release.version} ({'pinned manifest ' + src['manifest_sha256'][:16] if src['pinned'] else 'LOCAL, UNPINNED: ' + src['path']}"
           + (", DRY RUN" if src.get("dry_run") else "") + ")",
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
        out.append(("helios-oclp-post-install", "", "after installing: OCLP root patches for AppleHDA and the Kaby Lake HD 630",
                    "macOS 26 removed AppleHDA (AppleALC/NMHDA need it); docs/HARDWARE.md: HD 630 needs OCLP root patches on 14+"))
    if release is not None:
        out.append(("helios-usb-map", "", "UTBDefault + SSDT-HS07-UPC (Bluetooth port internal)",
                    "the scan has no physical port map; run USBToolBox after the install for USB 3 on every port"))
    return out

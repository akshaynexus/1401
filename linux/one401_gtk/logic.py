"""What the window reads from the engine and decides from it, without GTK, so it can be tested on its own.
Every function mirrors a piece of windows/App/MainForm.cs or Disks.cs; the comments name which."""
import hashlib
import json
import os
import re

from .loc import T, F

GiB = 1 << 30
MIN_STICK = 2 * GiB            # MainForm.MinStick (= usbwriter.MIN_DISK)
LARGE_STICK = 256 * GiB        # over this, the write needs --allow-large
BUS_NAMES = {1: "SCSI", 3: "ATA", 7: "USB", 8: "RAID", 10: "SAS", 11: "SATA", 12: "SD card", 13: "MMC card", 17: "NVMe"}


# --- engine output lines --------------------------------------------------------------------------------------------

def progress(line):
    """"PROGRESS <n> ..." -> n clamped to 0..100; None for any other line (MainForm.Say)."""
    if not line.startswith("PROGRESS "):
        return None
    parts = line.split(" ")
    try:
        return max(0, min(100, int(parts[1])))
    except (IndexError, ValueError):
        return -1   # a progress line that does not parse: still not shown in the log


def last_result(lines):
    """The last "RESULT ..." line, or None (nullmoth appcheck / appupdate / update)."""
    return next((l for l in reversed(lines) if l.startswith("RESULT ")), None)


def appcheck_newer(rc, lines):
    """The newer version when appcheck says "RESULT newer <v>", else None (offline/unknown never blocks)."""
    r = last_result(lines) or ""
    if rc != 0 or not r.startswith("RESULT newer "):
        return None
    return r[len("RESULT newer "):].strip() or None


def os_partitions(lines):
    """The text after "1401-OS-PARTITIONS:" in the writer's output, or None."""
    for l in lines:
        at = l.find("1401-OS-PARTITIONS:")
        if at >= 0:
            return l[at + len("1401-OS-PARTITIONS:"):].strip()
    return None


def disk_renumbered(lines):
    return any("1401-DISK-RENUMBERED" in l for l in lines)


def scan_changed(lines):
    return any("The scan changed after the EFI was built" in l for l in lines)


def doctor_lines(lines):
    """efidoctor's findings: the lines indented by two spaces."""
    return [l for l in lines if l.startswith("  ")]


def doctor_fixable(lines):
    return any(l.startswith("  needs fixing") for l in lines)


def diagnose_lines(lines):
    """p1401.diagnose's advice: the two-space lines without the indent."""
    return [l[2:] for l in lines if l.startswith("  ")]


# --- the build result -------------------------------------------------------------------------------------------------

def build_json(lines):
    """The JSON object --json prints, from the first line that starts with "{" (MainForm.OnNext, page 2). Raises
    ValueError when there is none or it does not parse."""
    text = "\n".join(_skip_until_brace(lines))
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("the build result is not an object")
    return value


def _skip_until_brace(lines):
    started = False
    for l in lines:
        if not started and l.lstrip().startswith("{"):
            started = True
        if started:
            yield l


def is_support_notice(text):
    """MainForm.IsSupportNotice: the engine notices shown under the summary."""
    return text.startswith(("NullMoth", "Graphics:", "Laptop display note:", "Intel I225-LM vP"))


def mac_name(darwin):
    return {"25": "Tahoe", "24": "Sequoia", "23": "Sonoma", "22": "Ventura", "21": "Monterey"}.get(darwin, darwin)


class BuildOutcome:
    """ok, error message (already worded for the log), macos_full, darwin, summary."""
    def __init__(self, ok, message="", macos_full="", darwin="", summary=""):
        self.ok, self.message, self.macos_full, self.darwin, self.summary = ok, message, macos_full, darwin, summary


def read_build(rc, lines):
    """The decision MainForm makes after `p1401 build --json`. Raises ValueError/KeyError/TypeError when the output
    cannot be read (the window then shows "Could not read the build result (<type>)")."""
    r = build_json(lines)
    if rc != 0 or r.get("ok") is not True:
        detail = "" if r.get("error") is None else str(r.get("error"))
        return BuildOutcome(False, T("The build stopped: ") + (detail if detail.strip() else F("Engine exit code {0}.", rc)))
    mv = str(r["macos_version"])
    darwin = mv.split(".")[0]
    summary = "macOS " + mac_name(darwin) + T(" for this PC, as a ") + str(r["smbios"]) + \
        T(". The startup files passed OpenCore's own check.")
    for n in r.get("notices") or []:
        if is_support_notice(str(n)):
            summary += "\n" + str(n)
    return BuildOutcome(True, macos_full=mv, darwin=darwin, summary=summary)


def build_args(report, acpi, efi, tahoe, experimental, boot_logs, stopped_at):
    """`p1401 build` exactly as MainForm passes it."""
    args = ["p1401", "build", report, acpi, efi, "--json", "--macos", "25" if tahoe else "24"]
    if experimental:
        args.append("--experimental-drivers")
    for f in boot_logs:
        args += ["--boot-log", f]
    if stopped_at:
        args += ["--stopped-at", stopped_at]
    return args


def guide_args(report, efi, macos_full, html):
    return ["p1401.guide", report, os.path.join(efi, "EFI", "OC", "config.plist"), "--macos", macos_full, "--html", html]


def firmware_guide_args(report, run_id, html):
    return ["p1401.guide", "--bios-only", report, "--run-id", run_id, "--html", html]


def scan_args(scan_dir, run_id):
    return ["p1401.scan", scan_dir, "--run-id", run_id]


def write_args(disk, efi, darwin, driver=None, extra=None, profile=None, arm_apps=False):
    """`p1401.usbwriter write` exactly as MainForm passes it."""
    args = ["p1401.usbwriter", "write", str(disk.number), efi, str(darwin),
            "--stick-id", disk.unique_id, "--stick-size", str(disk.size), "--stick-name", disk.name]
    if driver:
        args += ["--driver", driver]
    if extra:
        args += ["--extra", extra]
    if profile:
        args += ["--profile", profile]
    if disk.size > LARGE_STICK:
        args.append("--allow-large")
    if arm_apps:
        args.append("--arm-apps")
    return args


# --- facts from Report.json -------------------------------------------------------------------------------------------

def facts(report):
    """MainForm.LoadFacts: (label, value) rows."""
    rows = []
    mb = report.get("Motherboard")
    if isinstance(mb, dict):
        rows.append((T("Board"), str(mb["Name"])))
    cpu = report.get("CPU")
    if isinstance(cpu, dict):
        rows.append((T("Processor"), str(cpu["Processor Name"] if "Processor Name" in cpu else cpu["Manufacturer"])))
    for section, label in (("GPU", "Graphics"), ("Network", "Network")):
        value = report.get(section)
        if isinstance(value, dict):
            rows += [(T(label), str(k)) for k in value]
    return rows


# --- USB sticks -------------------------------------------------------------------------------------------------------

def gb(size):
    """C#'s {0:0.#} of size / 1e9."""
    s = f"{size / 1e9:.1f}"
    return s[:-2] if s.endswith(".0") else s


class UsbDisk:
    """One disk of `python -m p1401.usbwriter`'s JSON list, in the fields the write needs."""
    def __init__(self, raw):
        self.raw = raw
        self.number = int(raw.get("Number"))
        self.name = str(raw.get("FriendlyName") or "").strip()
        self.unique_id = str(raw.get("UniqueId") or "")
        self.size = int(raw.get("Size") or 0)
        bus = raw.get("BusType")
        self.bus = BUS_NAMES.get(bus, f"bus {bus}") if isinstance(bus, int) else str(bus or "unknown")
        self.device = device_path(raw)
        self.mounts = []

    @property
    def letters(self):
        """What Windows shows as drive letters: the device node and where its partitions are mounted."""
        parts = ([self.device] if self.device else []) + self.mounts
        return " ".join(parts)

    def __str__(self):
        return F("Disk {0}:  {1}  -  {2:0.#} GB{3}", self.number, self.name, gb(self.size),
                 "   (" + self.letters + ")" if self.letters else "").replace("{2:0.#}", gb(self.size))


def device_path(raw):
    """The /dev node a Linux disk list names, under whichever key the writer uses; None when it names none."""
    for key in ("Path", "DevicePath", "DevPath", "Device", "DevNode", "Node", "KName", "Name"):
        v = raw.get(key)
        if isinstance(v, str) and v:
            if v.startswith("/dev/"):
                return v
            if re.fullmatch(r"[a-z]+[a-z0-9]*", v) and os.path.exists("/sys/block/" + v):
                return "/dev/" + v
    return None


def parse_disk_list(output):
    """The JSON array the writer prints with no arguments; lines before it ("OK ..." notes) are skipped."""
    lines = output.splitlines()
    for i, l in enumerate(lines):
        if l.lstrip().startswith(("[", "{")):
            value = json.loads("\n".join(lines[i:]))
            if isinstance(value, dict):
                value = [value]
            if not isinstance(value, list):
                raise ValueError("the disk list is not a list")
            return [d for d in value if isinstance(d, dict) and d.get("Number") is not None]
    raise ValueError("the USB writer printed no disk list")


def offered_disks(raw_disks, self_devices=(), mounts=None):
    """Disks.ListUsb + MainForm.FillDisks: (offered UsbDisks sorted by number, reasons for the others).
    self_devices: /dev nodes 1401 and its working folder are on. mounts: {/dev node: [mount points]}."""
    offered, skipped = [], []
    mounts = mounts or {}
    for raw in raw_disks:
        try:
            d = UsbDisk(raw)
        except (TypeError, ValueError):
            continue
        if d.device:
            d.mounts = list(mounts.get(d.device, []))
        what = f"Disk {d.number} {d.name} ({gb(d.size)} GB)"
        if d.bus != "USB":
            skipped.append(what + T(": connected by ") + d.bus + T(", not USB"))
            continue
        if raw.get("IsSystem") or raw.get("IsBoot"):
            skipped.append(what + T(": Linux runs from it"))
            continue
        if d.device and d.device in self_devices:
            skipped.append(str(d) + T(": 1401 is running from it"))
            continue
        if d.size < MIN_STICK:
            skipped.append(str(d) + T(": too small, 1401 needs 2 GB"))
            continue
        # the Linux writer's own verdict ("Eligible"/"Reasons"); the size warning is answered by --allow-large
        reasons = [str(r) for r in raw.get("Reasons") or [] if "larger than any USB stick" not in str(r)]
        if raw.get("Eligible") is False and reasons:
            skipped.append(str(d) + ": " + "; ".join(reasons))
            continue
        offered.append(d)
    offered.sort(key=lambda d: d.number)
    return offered, skipped


# --- stick logs -------------------------------------------------------------------------------------------------------

def _glob(directory, prefix, suffix=".txt"):
    try:
        return [os.path.join(directory, n) for n in os.listdir(directory)
                if n.lower().startswith(prefix) and n.lower().endswith(suffix) and os.path.isfile(os.path.join(directory, n))]
    except OSError:
        return []


def stick_boot_logs(roots):
    """MainForm.StickBootLogs: per stick, the newest opencore-*.txt and the two newest panic-*.txt, from the root and
    NullMoth/sent-logs."""
    files, seen = [], set()
    for root in roots:
        if root in seen:
            continue
        seen.add(root)
        dirs = [d for d in (root, os.path.join(root, "NullMoth", "sent-logs")) if os.path.isdir(d)]
        oc = [f for d in dirs for f in _glob(d, "opencore-")]
        if oc:
            files.append(max(oc, key=lambda f: os.path.basename(f).lower()))
        panics = sorted((f for d in dirs for f in _glob(d, "panic-")), key=lambda f: os.path.basename(f).lower(), reverse=True)
        files += panics[:2]
    return files


def redact_config(xml):
    """MainForm.RedactConfig: the Mac identity in a config.plist, removed (data stays valid base64)."""
    keys = r"(<key>(SystemSerialNumber|MLB|BoardSerialNumber|ChassisSerialNumber|SystemUUID|ROM|SerialNumber)</key>\s*"
    xml = re.sub(keys + r"<string>)[^<]*(</string>)", r"\1REMOVED\3", xml)
    return re.sub(keys + r"<data>)[^<]*(</data>)", r"\1AAAAAAAA\3", xml)


def stick_provenance(root, config_path):
    """MainForm.StickProvenance."""
    try:
        profile = os.path.join(root, "NullMoth", "system-profile.json")
        if not os.path.isfile(profile):
            return "this stick's OpenCore EFI was not built by 1401"
        with open(profile, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        key = '"OC/config.plist":'
        at = text.find(key)
        if at < 0:
            return ""
        q1 = text.find('"', at + len(key))
        q2 = -1 if q1 < 0 else text.find('"', q1 + 1)
        if q1 < 0 or q2 < 0:
            return ""
        want = text[q1 + 1:q2]
        with open(config_path, "rb") as fh:
            have = hashlib.sha256(fh.read()).hexdigest()
        return "" if have == want else "this stick's OpenCore config was changed after 1401 built it"
    except OSError:
        return ""


def _vkey(text):
    m = re.fullmatch(r"(\d+)\.(\d+)(?:\.(\d+))?(?:\.(\d+))?", text)
    return tuple(int(g or 0) for g in m.groups()) if m else (0, 0, 0, 0)


def companion(dirs, prefix, suffix, own_version):
    """MainForm.Companion: this version's file first, else the highest version present, over every folder given."""
    files = []
    for d in dirs:
        try:
            files += [os.path.join(d, n) for n in os.listdir(d) if n.startswith(prefix) and n.endswith(suffix)]
        except OSError:
            pass
    if not files:
        return None
    exact = [f for f in files if os.path.basename(f).lower() == (prefix + own_version + suffix).lower()]
    if exact:
        return exact[0]
    return max(files, key=lambda f: _vkey(os.path.basename(f)[len(prefix):len(os.path.basename(f)) - len(suffix)]))

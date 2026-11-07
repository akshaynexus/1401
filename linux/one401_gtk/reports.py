"""Local diagnostic reports and the scan-evidence receipt: the Linux side of windows/App/DurableReport.cs and
ScanEvidence.cs. Saving never sends anything; files survive a restart and an upload failure. No GTK here."""
import datetime
import getpass
import json
import os
import re
import socket
import uuid

MAX_CHARACTERS = 500000
EVIDENCE_MAX_BYTES = 2 * 1024 * 1024
_RUN_TOKEN = re.compile(r"^[0-9a-f]{32}$")
_REPORT_NAME = re.compile(r"\A\d{8}T\d{9}Z-[a-z][a-z0-9-]{0,31}-[0-9a-f]{32}\.txt\Z")
_KIND = re.compile(r"\A[a-z][a-z0-9-]{0,31}\Z")

HARDWARE_VENDORS = {v.lower() for v in ("Apple", "Intel", "AMD", "NVIDIA", "Microsoft", "Realtek", "Broadcom", "Qualcomm",
                                         "MediaTek", "ASUS", "ASUSTeK", "ASRock", "Dell", "Lenovo", "HP", "Gigabyte", "MSI",
                                         "Acer", "AuthenticAMD", "GenuineIntel")}


class Outcome:
    def __init__(self, path=None, failure=None):
        self.path, self.failure = path, failure

    @property
    def saved(self):
        return self.path is not None


def _hardware_literal(text, m):
    """DurableReport.HardwareLiteral: a user or host name that is also a vendor word stays when it names hardware."""
    if m.group(0).lower() not in HARDWARE_VENDORS:
        return False
    lower = max(0, m.start() - 160)
    nl = text.rfind("\n", lower, m.start())
    start = max(lower, nl + 1)
    before = text[start:m.start()]
    after = text[m.end():m.end() + 96]
    if re.fullmatch(r"(?i)apple|nvidia|intel|microsoft|amd", m.group(0)) and \
            re.search(r"(?i)(?:^|[^a-z0-9_.-])com\.$", before) and after.startswith("."):
        return True
    if m.group(0).lower() == "nvidia" and (after.lower().startswith("-macos-driver") or (
            before.lower().endswith("nullmoth-") and re.match(r"^-[0-9]+\.[0-9]+\.[0-9]+\.tar\.gz", after))):
        return True
    if re.search(r"(?i)(?:[\"']?(?:manufacturer|vendor|board|motherboard|cpu|gpu|processor name)[\"']?\s*[:=]\s*[\"']?)$", before):
        return True
    return bool(re.match(r"(?i)^\s*(?:\(R\)|\(0x[0-9a-f]+\)|GeForce\b|Quadro\b|RTX\b|GTX\b|Ryzen\b|Radeon\b|Core\b|UHD\b|Iris\b|"
                         r"HD Graphics\b|Wi-Fi\b|Wireless\b|Ethernet\b)", after))


def clean_identity(value, user, host, home):
    """DurableReport.CleanIdentity for Linux paths: the home folder becomes $HOME, /home/<name> paths lose the name, and
    the user and host names become "user" and "this-pc"."""
    value = value or ""
    if home and len(home) > 3:
        value = re.sub(re.escape(home.rstrip("/")) + r"(?=/|$|[^\w.-])", "$HOME", value, flags=re.I)
    value = re.sub(r"/(?:home|var/home|Users)/[^/\r\n\"\t ]+", "$HOME", value)
    identities = [(user, "user", r"user(?:name)?|account|login"),
                  (host, "this-pc", r"host(?:name)?|computer(?: name)?|machine(?: name)?|pc")]
    for name, token, fields in identities:
        if not name or len(name) > 256:
            continue
        esc = re.escape(name)
        value = re.sub(r"(?i)(\b(?:" + fields + r")\b[\"']?\s*(?:[:=]\s*[\"']?|\s+))" + esc + r"(?![\w.-])",
                       lambda m, t=token: m.group(1) + t, value)
        if token == "this-pc":
            value = re.sub(r"(?i)(https?://)" + esc + r"(?=[/:?#]|$)", lambda m: m.group(1) + "this-pc", value)
    for name, token, _ in identities:
        if not name or len(name) <= 2 or len(name) > 256:
            continue
        pattern = re.compile(r"(?<!\w)" + re.escape(name) + r"(?!\w)", re.I)
        current = value
        value = pattern.sub(lambda m, t=token, c=current: m.group(0) if _hardware_literal(c, m) else t, current)
    return value


def clean(value):
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001
        user = ""
    return clean_identity(value, user, socket.gethostname(), os.path.expanduser("~"))


def _no_link(path):
    if os.path.islink(path):
        raise OSError("A diagnostic folder is a symbolic link.")


def save(work, kind, version, text):
    """DurableReport.Save: <work>/diagnostic-reports/<UTC stamp>-<kind>-<id>.txt, written to a .partial and renamed."""
    temporary = None
    try:
        if not _KIND.match(kind or ""):
            raise ValueError("Invalid diagnostic stage.")
        directory = os.path.join(work, "diagnostic-reports")
        _no_link(work)
        _no_link(directory)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        _no_link(directory)
        now = datetime.datetime.now(datetime.timezone.utc)
        stem = now.strftime("%Y%m%dT%H%M%S") + f"{now.microsecond // 1000:03d}Z-{kind}-{uuid.uuid4().hex}"
        path = os.path.join(directory, stem + ".txt")
        temporary = path + ".partial"
        raw = text or ""
        truncated = len(raw) > MAX_CHARACTERS
        body = clean(raw[:MAX_CHARACTERS] if truncated else raw)
        if truncated:
            body += "\n[report truncated at local diagnostic limit]\n"
        data = ("1401 " + clean(version) + " local " + kind + " report\n" + body).encode("utf-8")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.rename(temporary, path)
        temporary = None
        return Outcome(path=path)
    except Exception as error:  # noqa: BLE001
        return Outcome(failure=f"The local diagnostic report could not be saved ({type(error).__name__}, code "
                               f"{getattr(error, 'errno', 0) or 0}). Check free space and access to the app data folder, "
                               "then retry. No saved report is claimed.")
    finally:
        if temporary is not None:
            try:
                os.remove(temporary)
            except OSError:
                pass


def find(work):
    """The newest 12 saved reports (DurableReport.Find)."""
    directory = os.path.join(work, "diagnostic-reports")
    _no_link(directory)
    if not os.path.isdir(directory):
        return []
    names = [n for n in os.listdir(directory) if _REPORT_NAME.match(n) and not os.path.islink(os.path.join(directory, n))]
    return [os.path.join(directory, n) for n in sorted(names, reverse=True)[:12]]


def is_local(work, path):
    return os.path.dirname(os.path.abspath(path)) == os.path.abspath(os.path.join(work, "diagnostic-reports"))


# --- scan evidence (ScanEvidence.cs) -----------------------------------------------------------------------------------

def run_directory(work, token):
    if token is None or not _RUN_TOKEN.match(token):
        raise ValueError("Invalid scan run token.")
    return os.path.join(work, "scan-" + token)


def boot_time(stat_path="/proc/stat"):
    """Seconds since the epoch when Linux started (btime), or None."""
    try:
        with open(stat_path) as fh:
            for line in fh:
                if line.startswith("btime "):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def older_than_this_boot(directory, booted=None):
    """True when the run's Report.json was written before this boot (a firmware change needs a restart). One minute of
    slack, as on Windows."""
    try:
        if directory is None:
            return False
        report = os.path.join(directory, "Report.json")
        if not os.path.isfile(report):
            return False
        booted = boot_time() if booted is None else booted
        if booted is None:
            return False
        return os.path.getmtime(report) < booted - 60
    except OSError:
        return False


def remember(work, token):
    run_directory(work, token)
    os.makedirs(work, exist_ok=True)
    destination = os.path.join(work, "latest-scan.json")
    temporary = destination + ".tmp-" + uuid.uuid4().hex
    with open(temporary, "w", encoding="utf-8") as fh:
        json.dump({"schema": "nullmoth-latest-scan/1", "run_id": token}, fh)
    os.replace(temporary, destination)


def _read(path, maximum):
    try:
        if os.path.islink(path) or os.path.getsize(path) > maximum:
            return None
        with open(path, encoding="utf-8") as fh:
            value = json.load(fh)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


class Selection:
    def __init__(self, path, current_run):
        self.path, self.current_run = path, current_run


def find_evidence(work, current_token, allow_saved):
    """ScanEvidence.Find: the bounded receipt of this run, or (allow_saved) of the last remembered run."""
    token = current_token
    current = token is not None
    if not current:
        if not allow_saved:
            return None
        latest = _read(os.path.join(work, "latest-scan.json"), 4096)
        if not latest or latest.get("schema") != "nullmoth-latest-scan/1" or "run_id" not in latest:
            return None
        token = str(latest["run_id"])
    if not token or not _RUN_TOKEN.match(token):
        return None
    directory = run_directory(work, token)
    if os.path.islink(directory):
        return None
    path = os.path.join(directory, "scan-evidence.json")
    receipt = _read(path, EVIDENCE_MAX_BYTES)
    if not receipt or receipt.get("schema") != "nullmoth-scan-evidence/1" or str(receipt.get("run_id")) != token:
        return None
    return Selection(path, current)

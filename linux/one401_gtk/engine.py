"""Runs the 1401 engine and streams its output: windows/App/Engine.cs for Linux.

Every step is `python3 -E -s -B -X utf8 -u -m <module> <args>` from the engine folder (the folder holding p1401/ and
upstream/), the same interpreter options and environment as on Windows. Steps that need root (reading ACPI tables in
the scan, erasing and writing the stick) go through pkexec and linux/1401-helper, which checks the command, runs the
same module as root and gives the files it made back to the user. The window itself never runs as root.

No GTK here: on_line is called from the worker thread, and the caller marshals it."""
import hashlib
import os
import shlex
import shutil
import subprocess
import threading
import uuid

from . import reports
from .loc import T, engine_language

HERE = os.path.dirname(os.path.abspath(__file__))
LINUX_DIR = os.path.dirname(HERE)
HOME = os.path.dirname(LINUX_DIR)        # the engine folder: repo root, or the install folder
HELPER = os.path.join(LINUX_DIR, "1401-helper")
POLICY_ID = "com.nullmoth.A1401.helper"
POLICY_FILES = ("/usr/share/polkit-1/actions/com.nullmoth.A1401.policy",
                "/etc/polkit-1/actions/com.nullmoth.A1401.policy")
PYTHON_FLAGS = ["-E", "-s", "-B", "-X", "utf8", "-u"]   # Engine.PythonFlags
STRIPPED = ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE", "PYTHONEXECUTABLE", "PYTHONSAFEPATH")
# pkexec: 126 = the user dismissed the password prompt or is not allowed, 127 = no agent / authentication failed
PKEXEC_REFUSED = (126, 127)
HELPER_MARK = "OK administrator rights granted"


def xdg_data_home():
    value = os.environ.get("XDG_DATA_HOME", "")
    return value if os.path.isabs(value) else os.path.join(os.path.expanduser("~"), ".local", "share")


WORK = os.path.realpath(os.path.join(xdg_data_home(), "NullMoth", "1401"))   # %LOCALAPPDATA%\NullMoth\1401 on Windows


def python():
    """The system python3 (the one running this window, when it is a real python3)."""
    return shutil.which("python3") or "/usr/bin/python3"


def version():
    """The app version: linux/VERSION (written by install.sh), else the Windows project's <Version>."""
    try:
        with open(os.path.join(LINUX_DIR, "VERSION")) as fh:
            v = fh.read().strip()
            if v:
                return v
    except OSError:
        pass
    try:
        import re
        with open(os.path.join(HOME, "windows", "App", "App.csproj"), encoding="utf-8") as fh:
            m = re.search(r"<Version>([^<]+)</Version>", fh.read())
            if m:
                return m.group(1).strip()
    except OSError:
        pass
    return "0.0.0"


def nullmoth_dirs():
    """Where the NVIDIA driver package and the Mac app are kept: the install's NullMoth folder (a release unpacked
    beside the engine) and the working folder's, which Update the NVIDIA driver fills."""
    return [d for d in (os.path.join(HOME, "NullMoth"), os.path.join(WORK, "NullMoth"))]


def nullmoth_dir():
    """The folder Update the NVIDIA driver writes: the install's when it is writable, else the working folder's."""
    own = os.path.join(HOME, "NullMoth")
    if os.path.isdir(own) and os.access(own, os.W_OK):
        return own
    path = os.path.join(WORK, "NullMoth")
    os.makedirs(path, exist_ok=True)
    return path


def missing():
    """Engine.Missing, for a Linux folder."""
    if not os.path.isfile(os.path.join(HOME, "p1401", "__init__.py")):
        return T("The engine folder is incomplete (p1401 is missing next to linux/). Keep the whole 1401 folder together, "
                 "or run linux/install.sh again.")
    if not os.path.isdir(os.path.join(HOME, "upstream")):
        return T("The engine folder is incomplete (upstream/ is missing). Keep the whole 1401 folder together, "
                 "or run linux/install.sh again.")
    if not shutil.which("python3"):
        return T("python3 was not found. Install Python 3 (see docs/LINUX.md).")
    return None


def pkexec_path():
    return shutil.which("pkexec")


def policy_installed():
    return any(os.path.isfile(p) for p in POLICY_FILES)


def root_note():
    """A line on how root is obtained, for the log, or None when everything is in place."""
    if not pkexec_path():
        return T("pkexec (polkit) is not installed, so 1401 cannot ask for the administrator password it needs to read the "
                 "ACPI tables and write the stick. Install polkit (Arch: polkit, Fedora: polkit, Debian/Ubuntu: pkexec or "
                 "policykit-1) and a polkit agent, or run the command 1401 shows with sudo in a terminal.")
    if not policy_installed():
        return T("The 1401 polkit policy is not installed, so the password prompt shows a generic message. "
                 "Install it with linux/install.sh, or: sudo install -m 644 linux/com.nullmoth.A1401.policy "
                 "/usr/share/polkit-1/actions/ (the policy's exec.path must name this 1401-helper).")
    return None


def environment(privileged=False):
    env = {k: v for k, v in os.environ.items() if k not in STRIPPED}
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["A1401_LANG"] = engine_language()
    if not privileged:
        # engine.OCK_CACHE keeps its downloads under %LOCALAPPDATA%\NullMoth\1401 on Windows: the same place here
        env.setdefault("LOCALAPPDATA", xdg_data_home())
    return env


def command(args, privileged=False):
    """The argv that runs `-m <args>`: directly, or through pkexec and the helper."""
    if privileged:
        return [pkexec_path() or "pkexec", HELPER, "--lang", engine_language()] + list(args)
    return [python()] + PYTHON_FLAGS + ["-m"] + list(args)


def sudo_command(args):
    """What a person can type when pkexec is unavailable."""
    return "sudo " + " ".join(shlex.quote(a) for a in [HELPER, "--lang", engine_language()] + list(args))


def stage(args):
    a = list(args)
    if a[:1] == ["p1401.scan"]:
        return "scan"
    if a[:2] == ["p1401", "build"]:
        return "build"
    if a[:1] == ["p1401.usbwriter"]:
        return "write"
    return "operation"


def _fingerprints():
    text = ["fingerprint scope: files observed before this attempt; not executed-memory identity"]
    for name, path in (("app", os.path.join(LINUX_DIR, "1401")),
                       ("app-window", os.path.join(HERE, "app.py")),
                       ("helper", HELPER),
                       ("engine-source", os.path.join(HOME, "p1401", "engine.py")),
                       ("upstream-pin", os.path.join(HOME, "upstream", "PINNED.json"))):
        try:
            if not os.path.isfile(path) or os.path.islink(path) or os.path.getsize(path) > 32 * 1024 * 1024:
                text.append(name + ": unavailable")
                continue
            with open(path, "rb") as fh:
                text.append(name + ": SHA256 " + hashlib.sha256(fh.read()).hexdigest())
        except OSError as error:
            text.append(f"{name}: unavailable ({type(error).__name__}, code {error.errno})")
    return "\n".join(text) + "\n"


class Result:
    """The outcome of one run: exit code, every line, and the saved local report (Engine.LastReport)."""
    def __init__(self, rc, lines, report, refused=False):
        self.rc, self.lines, self.report, self.refused = rc, lines, report, refused


def run_blocking(args, on_line, privileged=False, ver=None, work=None, popen=subprocess.Popen):
    """Engine.Run: records the attempt, runs, streams every line to on_line, saves the final report."""
    work = work or WORK
    ver = ver or version()
    os.makedirs(work, exist_ok=True)
    attempt = uuid.uuid4().hex
    st = stage(args)
    op = " ".join(shlex.quote(a) for a in args)
    context = (f"app-reported distribution version: {ver} (Linux)\nengine identity: engine-source SHA256 observation "
               f"below; no separate engine version is declared\nattempt: {attempt}\nstage: {st}\noperation: {op}\n"
               f"privileged: {'pkexec' if privileged else 'no'}\n")
    started = reports.save(work, st + "-start", ver, context + "state: attempt recorded before execution\n")
    lines = []
    if not started.saved:
        on_line(started.failure + " The operation was not started.")
        return Result(1, lines, started)
    out, size, truncated = [], 0, False

    def record(line):
        nonlocal size, truncated
        lines.append(line)
        left = reports.MAX_CHARACTERS - size
        if left > 0:
            out.append(line[:left])
            size += min(len(line), left) + 1
        if len(line) > left:
            truncated = True
        try:
            on_line(line)
        except Exception as notification:  # noqa: BLE001
            out.append(f"Output notification failed ({type(notification).__name__}); local reporting continues.")

    rc, refused = 1, False
    try:
        context += _fingerprints()
        if privileged and not pkexec_path():
            record(root_note())
            record(T("To run this step by hand, open a terminal and run:"))
            record("  " + sudo_command(args))
            return_code = 1
        else:
            p = popen(command(args, privileged), cwd=HOME, env=environment(privileged), stdin=subprocess.DEVNULL,
                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                      bufsize=1)
            with p:
                for raw in p.stdout:
                    record(raw.rstrip("\r\n"))
                return_code = p.wait()
            if privileged and return_code in PKEXEC_REFUSED and not any(l.startswith(HELPER_MARK) for l in lines):
                refused = True
                record(T("The administrator password was not given, so this step did not run. Nothing was changed."))
                if return_code == 127:
                    record(T("If no password window appeared, no polkit agent is running in this session. Log in to a "
                             "desktop with one (GNOME, KDE and most desktops start it), or run the step by hand:"))
                    record("  " + sudo_command(args))
        rc = return_code
    except Exception as error:  # noqa: BLE001
        record(f"Engine operation failed ({type(error).__name__}). Check the complete 1401 folder and send the saved "
               "local report.")
    finally:
        saved = context + f"exit code: {rc}\n" + "\n".join(out) + ("\n[output truncated]" if truncated else "")
        final = reports.save(work, st + "-final", ver, saved)
    return Result(rc, lines, final, refused)


def run_in_thread(args, on_line, done, privileged=False):
    """run_blocking on a worker thread; done(Result) is called on that thread."""
    def work():
        done(run_blocking(args, on_line, privileged))
    t = threading.Thread(target=work, daemon=True)
    t.start()
    return t


def list_disks(timeout=30):
    """`python -m p1401.usbwriter` with no arguments: the writer's own JSON disk list (unprivileged). Returns
    (rc, output); rc None on timeout."""
    try:
        p = subprocess.run(command(["p1401.usbwriter"]), cwd=HOME, env=environment(), stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return p.returncode, p.stdout + ("\n" + p.stderr if p.returncode else "")
    except subprocess.TimeoutExpired:
        return None, ""

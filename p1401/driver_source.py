"""Which NVIDIA driver goes on the stick: NullMoth's own release (the default) or a fork of it named in a config.

With no config this module changes nothing: nullmoth.py keeps its pinned package, NullMoth's release list and NullMoth's
release key, byte for byte. A config names another GitHub repository (a fork of nullmoth/nvidia-macos-driver) and,
optionally, a pinned package, the fork's own Ed25519 release key, and a card table of its own.

Where the config is read from, first match wins:
  1. P1401_DRIVER_SOURCE = path to a JSON file (it must exist and parse; a broken one stops the build, never falls back),
     or the word "official": NullMoth's pinned package whatever the config folder holds (also for test runs)
  2. NULLMOTH_DRIVER_REPO = owner/name or a github.com URL, with NULLMOTH_DRIVER_KEY (base64 Ed25519 public key) and
     NULLMOTH_DRIVER_ALLOW_UNSIGNED=1 as the only other settings
  3. driver-source.json in 1401's config folder: %APPDATA%\\NullMoth\\1401 on Windows,
     ~/Library/Application Support/NullMoth/1401 on macOS, $XDG_CONFIG_HOME/NullMoth/1401 (~/.config) elsewhere

The JSON (only "repo" is required):
  {"repo": "someone/nvidia-macos-driver",
   "tag": "v2.8.0",                                  # a release by tag; omitted = the repository's latest release
   "package": {"name": "nullmoth-nvidia-2.8.0.tar.gz",  # optional pin; must be in that release's SHA256SUMS.txt
               "url": "https://github.com/someone/nvidia-macos-driver/releases/download/v2.8.0/nullmoth-nvidia-2.8.0.tar.gz",
               "sha256": "<64 hex>"},
   "public_key": "<base64 of the fork's raw 32-byte Ed25519 public key>",
   "allow_unsigned": false,
   "cards": "nvidia_gsp_ids.json" | {"ids": {"2D05": "GeForce RTX 5060"}, "tested": ["2D05"]},
   "cards_mode": "merge" | "replace"}

What a fork must pass (nothing is dropped silently): the package is listed with its SHA-256 in SHA256SUMS.txt of the
same release, and it carries a detached signature (<name>.sig over the bytes, or <name>.bsig bound to product, version
and hash, as NullMoth's releases do) by the key in the config. NullMoth's key is never assumed for a fork: a fork
package passes it only when NullMoth actually signed it. Without a key, "allow_unsigned": true is required, and the build
log and the stick's README then say in capitals that the package is unsigned."""
import json
import os
import re
import sys

OFFICIAL_REPO = "nullmoth/nvidia-macos-driver"
ENV_FILE = "P1401_DRIVER_SOURCE"
ENV_REPO = "NULLMOTH_DRIVER_REPO"
ENV_KEY = "NULLMOTH_DRIVER_KEY"
ENV_UNSIGNED = "NULLMOTH_DRIVER_ALLOW_UNSIGNED"
FILE_NAME = "driver-source.json"
STICK_MANIFEST = "driver-source.json"  # beside the package on the stick, read by the Mac app (DriverSource.swift)
PACKAGE_RE = re.compile(r"^nullmoth-nvidia-(\d+\.\d+\.\d+)\.tar\.gz$")
_REPO_RE = re.compile(r"^([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9._-]{1,100})$")
_TAG_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_KEYS = {"repo", "tag", "package", "public_key", "allow_unsigned", "cards", "cards_mode", "comment"}


class SourceError(RuntimeError):
    pass


def config_path(env=None, platform=None):
    env = os.environ if env is None else env
    platform = platform or sys.platform
    home = env.get("USERPROFILE") or env.get("HOME") or os.path.expanduser("~")
    if platform.startswith("win"):
        base = env.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
    elif platform == "darwin":
        base = os.path.join(home, "Library", "Application Support")
    else:
        base = env.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    return os.path.join(base, "NullMoth", "1401", FILE_NAME)


def parse_repo(text):
    """owner/name from "owner/name", "github.com/owner/name" or "https://github.com/owner/name(.git)(/)"."""
    s = str(text or "").strip()
    s = re.sub(r"^https://(www\.)?github\.com/", "", s, flags=re.I)
    s = re.sub(r"^(www\.)?github\.com/", "", s, flags=re.I)
    s = s.rstrip("/")
    s = s[:-4] if s.endswith(".git") else s
    m = _REPO_RE.match(s)
    if not m or m.group(2) in (".", ".."):
        raise SourceError(f"repo {text!r} is not a GitHub repository (owner/name or https://github.com/owner/name)")
    return f"{m.group(1)}/{m.group(2)}"


class Source:
    """The driver source in use. official+builtin = the package pinned in nullmoth.py (no config at all)."""

    def __init__(self, repo=OFFICIAL_REPO, tag=None, package=None, public_key=None, allow_unsigned=False,
                 cards=None, cards_mode="merge", origin=None):
        self.repo, self.tag, self.package, self.public_key = repo, tag, package, public_key
        self.allow_unsigned, self.cards, self.cards_mode, self.origin = allow_unsigned, cards, cards_mode, origin

    @property
    def official(self):
        return self.repo.lower() == OFFICIAL_REPO

    @property
    def builtin(self):
        """No config: nullmoth.PACKAGE, as before."""
        return self.origin is None

    def key(self, official_key):
        """The key a package from this source must be signed with, or None (allow_unsigned fork)."""
        return official_key if self.official else self.public_key

    def release_api(self):
        base = f"https://api.github.com/repos/{self.repo}/releases/"
        return base + ("tags/" + self.tag if self.tag else "latest")

    def label(self):
        if self.builtin:
            return f"NullMoth official ({OFFICIAL_REPO}, pinned in 1401)"
        who = "NullMoth official" if self.official else "FORK"
        sig = ("NullMoth release key" if self.official else
               f"fork key {self.public_key[:12]}..." if self.public_key else "UNSIGNED (allow_unsigned)")
        return f"{who} github.com/{self.repo}" + (f" {self.tag}" if self.tag else "") + f", signature: {sig}, config: {self.origin}"

    def info(self):
        return {"repo": self.repo, "official": self.official, "builtin": self.builtin, "tag": self.tag,
                "package": dict(self.package) if self.package else None, "public_key": self.public_key,
                "allow_unsigned": self.allow_unsigned, "cards_mode": self.cards_mode if self.cards else None,
                "config": self.origin}


DEFAULT = Source()


def _bool(v, what):
    if not isinstance(v, bool):
        raise SourceError(f"{what} must be true or false")
    return v


def _key(v):
    import base64  # noqa: PLC0415
    try:
        raw = base64.b64decode(str(v).strip(), validate=True)
    except (ValueError, TypeError):
        raw = b""
    if len(raw) != 32:
        raise SourceError("public_key must be base64 of a raw 32-byte Ed25519 public key")
    return base64.b64encode(raw).decode()


def _cards(v, base_dir):
    if isinstance(v, str):
        p = v if os.path.isabs(v) else os.path.join(base_dir or ".", v)
        try:
            with open(p, encoding="utf-8") as fh:
                v = json.load(fh)
        except (OSError, ValueError) as e:
            raise SourceError(f"the card table {p} could not be read: {e}") from None
    if not isinstance(v, dict) or not isinstance(v.get("ids"), dict):
        raise SourceError('cards must be a card table {"ids": {"2D05": "name", ...}, "tested": [...]} or a path to one')
    ids = {}
    for k, name in v["ids"].items():
        if not (isinstance(k, str) and re.fullmatch(r"[0-9A-Fa-f]{4}", k) and isinstance(name, str)):
            raise SourceError(f"card id {k!r} is not a 4-digit hex PCI device id with a name")
        ids[k.upper()] = name
    tested = v.get("tested") or []
    if not isinstance(tested, list) or not all(isinstance(t, str) and re.fullmatch(r"[0-9A-Fa-f]{4}", t) for t in tested):
        raise SourceError("cards.tested must be a list of 4-digit hex PCI device ids")
    return {"source": str(v.get("source") or "driver fork card table"), "tested": [t.upper() for t in tested], "ids": ids}


def from_dict(d, origin, base_dir=None):
    if not isinstance(d, dict):
        raise SourceError("the driver source config must be a JSON object")
    extra = set(d) - _KEYS
    if extra:
        raise SourceError(f"unknown setting(s) {', '.join(sorted(extra))}")
    repo = parse_repo(d.get("repo"))
    tag = d.get("tag")
    if tag is not None and not (isinstance(tag, str) and _TAG_RE.match(tag)):
        raise SourceError("tag must be a release tag such as v2.8.0")
    key = _key(d["public_key"]) if d.get("public_key") else None
    unsigned = _bool(d.get("allow_unsigned", False), "allow_unsigned")
    pkg = d.get("package")
    if pkg is not None:
        if not isinstance(pkg, dict) or not PACKAGE_RE.match(str(pkg.get("name", ""))):
            raise SourceError("package.name must be nullmoth-nvidia-X.Y.Z.tar.gz (the name the Mac app and setup expect)")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", str(pkg.get("sha256", ""))):
            raise SourceError("package.sha256 must be 64 hex digits")
        url = str(pkg.get("url") or "")
        m = re.match(r"^https://github\.com/([^/]+/[^/]+)/releases/download/([^/]+)/([^/]+)$", url)
        if not m or m.group(1).lower() != repo.lower() or m.group(3) != pkg["name"]:
            raise SourceError(f"package.url must be https://github.com/{repo}/releases/download/<tag>/{pkg['name']}")
        if tag is not None and m.group(2) != tag:
            raise SourceError("package.url is from another release than tag")
        tag = m.group(2)
        pkg = {"name": pkg["name"], "url": url, "sha256": pkg["sha256"].lower(), "version": PACKAGE_RE.match(pkg["name"]).group(1)}
    mode = d.get("cards_mode", "merge")
    if mode not in ("merge", "replace"):
        raise SourceError('cards_mode must be "merge" or "replace"')
    cards = _cards(d["cards"], base_dir) if d.get("cards") is not None else None
    src = Source(repo, tag, pkg, key, unsigned, cards, mode, origin)
    if src.official:
        # NullMoth's own repository is only ever checked with NullMoth's key; there is no unsigned NullMoth release.
        if unsigned:
            raise SourceError(f"allow_unsigned cannot be used with {OFFICIAL_REPO}")
        if key is not None:
            from . import release_sig  # noqa: PLC0415
            if key != release_sig.PUBLIC_KEY:
                raise SourceError(f"public_key is not NullMoth's release key; {OFFICIAL_REPO} is checked only with it")
    elif key is None and not unsigned:
        raise SourceError(f"github.com/{repo} is a fork: give its public_key, or set \"allow_unsigned\": true to accept "
                          "its packages with no signature at all (they run as root on the Mac)")
    return src


def load(env=None, platform=None):
    """The configured Source, or DEFAULT. Raises SourceError for a config that is there but wrong."""
    env = os.environ if env is None else env
    path = env.get(ENV_FILE)
    if path and path.strip().lower() == "official":  # NullMoth's pinned package whatever the config folder holds
        return DEFAULT
    if path:
        if not os.path.isfile(path):
            raise SourceError(f"{ENV_FILE} names {path}, which is not a file")
    elif env.get(ENV_REPO):
        d = {"repo": env[ENV_REPO]}
        if env.get(ENV_KEY):
            d["public_key"] = env[ENV_KEY]
        if env.get(ENV_UNSIGNED):
            d["allow_unsigned"] = env[ENV_UNSIGNED].strip().lower() in ("1", "true", "yes")
        return from_dict(d, f"${ENV_REPO}")
    else:
        path = config_path(env, platform)
        if not os.path.isfile(path):
            return DEFAULT
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError) as e:
        raise SourceError(f"{path} could not be read: {e}") from None
    try:
        return from_dict(d, path, os.path.dirname(os.path.abspath(path)))
    except SourceError as e:
        raise SourceError(f"{path}: {e}") from None


def load_safe(env=None, platform=None):
    """(Source, None) or (DEFAULT, the error): importing nullmoth must not fail on a bad config; stage() raises it."""
    try:
        return load(env, platform), None
    except SourceError as e:
        return DEFAULT, str(e)


def card_table(default, src):
    """The card table mark()/apply() use: the default one, the fork's merged over it, or the fork's alone."""
    if src is None or not src.cards:
        return default
    if src.cards_mode == "replace":
        return dict(src.cards)
    ids = dict(default.get("ids") or {})
    ids.update(src.cards["ids"])
    tested = list(default.get("tested") or []) + [t for t in src.cards["tested"] if t not in (default.get("tested") or [])]
    return {"source": f"{default.get('source', '')} + {src.cards['source']}", "tested": tested, "ids": ids}

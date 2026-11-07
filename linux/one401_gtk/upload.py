"""Sends one text file to nullmothsystems.com's upload endpoint (MainForm.Send), only after the person agreed in the
window. Returns the report ID, or None with the reason in .last_error."""
import base64
import hashlib
import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request

URL = "https://nullmothsystems.com/api/upload"


def new_batch():
    """One random value per send, shared by every file of that send (MainForm.NewBatch)."""
    return "1401-app-" + secrets.token_hex(8)


def request(path, send_name, what, version, batch=None):
    """The urllib Request for one file, and the SHA-256 of the bytes it carries."""
    with open(path, "rb") as fh:
        body = fh.read()
    if send_name.lower().endswith(".txt"):
        body = body.replace(b"\0", b"")   # the site refuses a text log with NUL bytes (OpenCore pads its log)
    sha = hashlib.sha256(body).hexdigest()
    notes = f"1401 {version} {what} (sent from the Linux app)"[:3900]
    meta = json.dumps({"consent": True, "notes": notes, "batch": batch or new_batch()})
    b64 = base64.urlsafe_b64encode(meta.encode("utf-8")).decode("ascii").rstrip("=")
    req = urllib.request.Request(URL, data=body, method="POST", headers={
        "Content-Type": "application/octet-stream", "X-File-Name": urllib.parse.quote(send_name, safe=""),
        "X-Meta": b64, "X-Content-SHA256": sha, "User-Agent": "1401"})
    return req, sha


class Sender:
    def __init__(self, version):
        self.version = version
        self.last_error = ""

    def send(self, path, send_name, what, batch=None, opener=urllib.request.urlopen):
        self.last_error = ""
        try:
            req, sha = request(path, send_name, what, self.version, batch)
            with opener(req, timeout=30) as resp:
                j = json.loads(resp.read().decode("utf-8", "replace") or "{}")
            if "sha256" in j and str(j["sha256"]) != sha:
                self.last_error = "the server stored different bytes"
                return None
            return str(j["id"]) if "id" in j else "?"
        except urllib.error.HTTPError as e:
            self.last_error = str(e)
            try:
                j = json.loads(e.read().decode("utf-8", "replace"))
                if "error" in j:
                    self.last_error = str(j["error"])
            except Exception:  # noqa: BLE001
                pass
            return None
        except Exception as e:  # noqa: BLE001 - the reason is what the person sees
            self.last_error = str(getattr(e, "reason", "") or e)
            return None


def send_name(path):
    name = os.path.basename(path)
    return name + ".txt" if name.lower().endswith(".json") else name

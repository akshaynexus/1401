"""The hardware map (windows/App/HardwareMap.cs) read from Linux's sysfs and /proc/asound, without root.

Same file, same field names, same rule: an allow-list of fixed-width numbers, each masked to its width, written in name
order. Nothing else is read: no serial numbers (USB "serial" files are never opened), no names, no MAC addresses.
An absent field means it could not be read, never that the device is absent or supported."""
import glob
import os
import re

from .loc import F

MAX_DEVICES = 512
MAX_EXAMINED = 4096
MAX_ACPI_IDS = 64
ACPI_SHAPE = re.compile(r"^(?:PNP[0-9A-F]{4}|ACPI[0-9A-F]{4}|[A-Z]{3}[0-9A-F]{4}|[A-Z]{4}[0-9A-F]{4})$")


def _hex(path, width_mask):
    try:
        with open(path) as fh:
            text = fh.read(64).strip()
        return int(text, 16) & width_mask
    except (OSError, ValueError):
        return None


def acpi_ids(values):
    """HardwareMap.AcpiIDs: the shapes the Mac app accepts, last segment, sorted, capped."""
    kept = set()
    for raw in values or []:
        if len(kept) >= MAX_ACPI_IDS or not raw or len(raw) > 256:
            continue
        text = raw.upper()
        cut = max(text.rfind("\\"), text.rfind("*"), text.rfind(":"))
        if cut >= 0:
            text = text[cut + 1:]
        if ACPI_SHAPE.match(text):
            kept.add(text)
    return sorted(kept)


def line(facts, acpi=None):
    """HardwareMap.Line."""
    out = "device" + "".join(f" {k}={facts[k]}" for k in sorted(facts))
    if acpi:
        out += " acpi_ids=" + ",".join(acpi)
    return out


def _put(facts, name, value):
    if value is not None:
        facts[name] = value


def pci_device(path):
    f = {}
    _put(f, "pci_vendor", _hex(os.path.join(path, "vendor"), 0xffff))
    _put(f, "pci_device", _hex(os.path.join(path, "device"), 0xffff))
    _put(f, "pci_subsystem_vendor", _hex(os.path.join(path, "subsystem_vendor"), 0xffff))
    _put(f, "pci_subsystem_device", _hex(os.path.join(path, "subsystem_device"), 0xffff))
    _put(f, "pci_revision", _hex(os.path.join(path, "revision"), 0xff))
    _put(f, "pci_class", _hex(os.path.join(path, "class"), 0xffffff))
    return f


def usb_device(path):
    """A USB device (idVendor) or one of its interfaces (bInterfaceNumber, vendor and product from the parent)."""
    f = {}
    if os.path.exists(os.path.join(path, "bInterfaceNumber")):
        parent = os.path.dirname(os.path.realpath(path))
        _put(f, "usb_vendor", _hex(os.path.join(parent, "idVendor"), 0xffff))
        _put(f, "usb_product", _hex(os.path.join(parent, "idProduct"), 0xffff))
        _put(f, "usb_interface_number", _hex(os.path.join(path, "bInterfaceNumber"), 0xff))
        _put(f, "usb_class", _hex(os.path.join(path, "bInterfaceClass"), 0xff))
        _put(f, "usb_subclass", _hex(os.path.join(path, "bInterfaceSubClass"), 0xff))
        _put(f, "usb_protocol", _hex(os.path.join(path, "bInterfaceProtocol"), 0xff))
    elif os.path.exists(os.path.join(path, "idVendor")):
        _put(f, "usb_vendor", _hex(os.path.join(path, "idVendor"), 0xffff))
        _put(f, "usb_product", _hex(os.path.join(path, "idProduct"), 0xffff))
        _put(f, "usb_class", _hex(os.path.join(path, "bDeviceClass"), 0xff))
        _put(f, "usb_subclass", _hex(os.path.join(path, "bDeviceSubClass"), 0xff))
        _put(f, "usb_protocol", _hex(os.path.join(path, "bDeviceProtocol"), 0xff))
    return f


def hid_usage(descriptor):
    """The top-level usage page and usage before the first collection of a HID report descriptor."""
    page = usage = None
    i = 0
    while i < len(descriptor) and i < 4096:
        prefix = descriptor[i]
        if prefix == 0xFE:   # long item
            if i + 1 >= len(descriptor):
                break
            i += 3 + descriptor[i + 1]
            continue
        size = (0, 1, 2, 4)[prefix & 3]
        data = int.from_bytes(descriptor[i + 1:i + 1 + size], "little")
        tag = prefix & 0xFC
        if tag == 0x04:
            page = data & 0xffff
        elif tag == 0x08:
            usage = data & 0xffff
        elif tag == 0xA0:
            break
        i += 1 + size
    return page, usage


def hid_device(path):
    f = {}
    m = re.match(r"^[0-9A-Fa-f]{4}:([0-9A-Fa-f]{4}):([0-9A-Fa-f]{4})\.", os.path.basename(path))
    if m:
        f["hid_vendor"] = int(m.group(1), 16) & 0xffff
        f["hid_product"] = int(m.group(2), 16) & 0xffff
    try:
        with open(os.path.join(path, "report_descriptor"), "rb") as fh:
            page, usage = hid_usage(fh.read(4096))
        _put(f, "hid_usage_page", page)
        _put(f, "hid_usage", usage)
    except OSError:
        pass
    return f


def codec(text):
    """One /proc/asound/card*/codec#* file: the codec's 32-bit vendor/device id and its audio function group."""
    f = {}
    m = re.search(r"^Vendor Id:\s*0x([0-9a-fA-F]{8})\s*$", text, re.M)
    if m:
        f["audio_codec_vendor_device"] = int(m.group(1), 16) & 0xffffffff
        if re.search(r"^Node 0x01 \[Audio Function Group\]", text, re.M) or "Audio Function Group" in text:
            f["audio_codec_function_group"] = 1
    return f


def acpi_device(path):
    values = []
    try:
        with open(os.path.join(path, "modalias")) as fh:
            values = fh.read(512).strip().split(":")[1:]
    except OSError:
        pass
    try:
        with open(os.path.join(path, "hid")) as fh:
            values.append(fh.read(64).strip())
    except OSError:
        pass
    return acpi_ids(values)


def collect(sys_root="/sys", proc_root="/proc"):
    """HardwareMap.Collect."""
    out = ["hardware map 1"]
    listed = silent = examined = 0
    sources = []
    sources += [("pci", p) for p in sorted(glob.glob(os.path.join(sys_root, "bus/pci/devices/*")))]
    sources += [("usb", p) for p in sorted(glob.glob(os.path.join(sys_root, "bus/usb/devices/*")))]
    sources += [("hid", p) for p in sorted(glob.glob(os.path.join(sys_root, "bus/hid/devices/*")))]
    sources += [("codec", p) for p in sorted(glob.glob(os.path.join(proc_root, "asound/card*/codec#*")))]
    sources += [("acpi", p) for p in sorted(glob.glob(os.path.join(sys_root, "bus/acpi/devices/*")))]
    if not sources:
        out.append("devices_status: unavailable: no sysfs device tree")
    for kind, path in sources:
        if listed >= MAX_DEVICES:
            out.append(f"devices_truncated: {MAX_DEVICES}")
            break
        examined += 1
        if examined > MAX_EXAMINED:
            out.append(f"devices_examined_truncated: {MAX_EXAMINED}")
            break
        facts, acpi = {}, []
        try:
            if kind == "pci":
                facts = pci_device(path)
                acpi = acpi_device(os.path.join(path, "firmware_node"))
            elif kind == "usb":
                facts = usb_device(path)
            elif kind == "hid":
                facts = hid_device(path)
            elif kind == "codec":
                with open(path, errors="replace") as fh:
                    facts = codec(fh.read(8192))
            else:
                acpi = acpi_device(path)
        except OSError:
            silent += 1
            continue
        if not facts and not acpi:
            silent += 1
            continue
        out.append(line(facts, acpi))
        listed += 1
    out.append(f"devices_listed: {listed}")
    out.append(f"devices_without_identifiers: {silent}")
    return "\n".join(out) + "\n"


def write_to(directory):
    """HardwareMap.WriteTo: hardware-map.txt beside Report.json; returns the one line shown in the log."""
    try:
        text = collect()
    except Exception as error:  # noqa: BLE001 - the scan it rides along with is worth more than the map
        return F("Hardware map: not collected ({0}).", type(error).__name__)
    devices = sum(1 for l in text.split("\n") if l.startswith("device "))
    try:
        with open(os.path.join(directory, "hardware-map.txt"), "w", encoding="utf-8") as fh:
            fh.write(text)
        return F("Hardware map: {0} device(s) with identifiers, written to hardware-map.txt.", devices)
    except PermissionError:
        return F("Hardware map: read {0} device(s) but could not save it (access denied).", devices)
    except OSError as error:
        return F("Hardware map: read {0} device(s) but could not save it ({1}).", devices, type(error).__name__)

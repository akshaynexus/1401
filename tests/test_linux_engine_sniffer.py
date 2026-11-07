"""The Hardware Sniffer's Linux collector, corrected to report what its Windows collector reports: physical cores (not
logical CPUs), the PCI/ACPI paths of network adapters, only \\_SB ACPI paths, and RAID/Intel VMD controllers that own
the drives behind them. Each case uses a fake sysfs tree, so it runs on any OS."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from p1401 import hwcapture, linuxhost


class Tmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def cpu_tree(self, cores):
        """cores: [(package, [logical cpus])]"""
        sys_cpu = self.root / "cpu"
        for package, lps in cores:
            for lp in lps:
                top = sys_cpu / f"cpu{lp}" / "topology"
                top.mkdir(parents=True)
                (top / "core_cpus_list").write_text(",".join(map(str, lps)) + "\n")
                (top / "physical_package_id").write_text(f"{package}\n")
        (sys_cpu / "cpufreq").mkdir(parents=True, exist_ok=True)   # not a cpuN folder
        return sys_cpu


class Cores(Tmp):
    def test_hybrid_intel_counts_physical_cores(self):
        # i9-12900H: 6 P-cores with SMT (12 threads) + 8 E-cores = 14 cores, 20 logical CPUs
        layout = [(0, [2 * i, 2 * i + 1]) for i in range(6)] + [(0, [12 + i]) for i in range(8)]
        sys_cpu = self.cpu_tree(layout)
        self.assertEqual(linuxhost.physical_cores(str(sys_cpu), cpuinfo=str(self.root / "none")), 14)
        topo = linuxhost.cpu_topology(str(sys_cpu))
        self.assertEqual((topo["packages"], topo["cores"], topo["logical_processors"]), (1, 14, 20))
        self.assertEqual(sum(c["smt"] for c in topo["core_map"]), 6)
        self.assertEqual(topo["cores_per_package"], [14])

    def test_two_sockets_sum_like_windows(self):
        layout = [(p, [p * 8 + 2 * i, p * 8 + 2 * i + 1]) for p in (0, 1) for i in range(4)]
        sys_cpu = self.cpu_tree(layout)
        self.assertEqual(linuxhost.physical_cores(str(sys_cpu)), 8)
        self.assertEqual(linuxhost.cpu_topology(str(sys_cpu))["cores_per_package"], [4, 4])

    def test_proc_cpuinfo_is_the_fallback(self):
        info = self.root / "cpuinfo"
        blocks = [f"processor\t: {n}\nphysical id\t: 0\ncore id\t\t: {n // 2}\n" for n in range(8)]
        info.write_text("\n".join(blocks))
        self.assertEqual(linuxhost.physical_cores(str(self.root / "nocpu"), cpuinfo=str(info)), 4)
        self.assertIsNone(linuxhost.physical_cores(str(self.root / "nocpu"), cpuinfo=str(self.root / "none")))

    @unittest.skipUnless(os.path.isdir("/sys/devices/system/cpu/cpu0/topology"), "needs a Linux sysfs")
    def test_hwcapture_measures_the_topology_on_linux(self):
        with mock.patch.object(hwcapture.sys, "platform", "linux"):
            record = hwcapture.cpu_topology()
        self.assertEqual(record["status"], "measured")
        self.assertGreaterEqual(record["value"]["logical_processors"], record["value"]["cores"])

    def test_hwcapture_reports_a_failed_sysfs_read(self):
        with mock.patch.object(hwcapture.sys, "platform", "linux"), \
                mock.patch.object(hwcapture, "_owned_module", return_value=SimpleNamespace(cpu_topology=lambda: (_ for _ in ()).throw(OSError("no sysfs")))):
            record = hwcapture.cpu_topology()
        self.assertEqual(record["status"], "unavailable")
        self.assertIn("no sysfs", record["error"])


class FakeInfo:
    """The parts of upstream LinuxHardwareInfo that patch_sniffer wraps."""
    def __init__(self, devices_by_class=None, locations=None):
        self.devices_by_class = devices_by_class or {}
        self.locations = locations or {}
        self.storage_result = {}
        self.net_dirs = []

    def get_device_location_paths(self, device_dir):
        return dict(self.locations.get(device_dir, {}))

    def cpu(self):
        return {"Manufacturer": "Intel", "Core Count": "20"}

    def network(self):
        # upstream passes /sys/class/net/<if> itself to the locator
        return {os.path.basename(d): self.get_device_location_paths(d) for d in self.net_dirs}

    def storage_controllers(self):
        return dict(self.storage_result)

    def system_devices(self):
        return {"Fan": {"Bus Type": "ACPI", **self.get_device_location_paths("/fan")},
                "EC": {"Bus Type": "ACPI", **self.get_device_location_paths("/ec")}}


class PatchSniffer(Tmp):
    def test_core_count_is_physical_cores(self):
        info = linuxhost.patch_sniffer(FakeInfo(), cores=lambda: 14)
        self.assertEqual(info.cpu()["Core Count"], "14")
        info = linuxhost.patch_sniffer(FakeInfo(), cores=lambda: None)
        self.assertEqual(info.cpu()["Core Count"], "20")   # unmeasured: the collector's value stays

    def test_network_adapters_get_their_pci_device_paths(self):
        wlan = os.path.join(str(self.root), "net", "wlan0")
        os.makedirs(os.path.join(wlan, "device"))
        info = FakeInfo(locations={
            os.path.join(wlan, "device"): {"PCI Path": "PciRoot(0x0)/Pci(0x14,0x3)", "ACPI Path": "\\_SB.PC00.CNVW"}})
        info.net_dirs = [wlan, os.path.join(str(self.root), "net", "usb0")]
        linuxhost.patch_sniffer(info)
        located = info.get_device_location_paths
        out = info.network()
        self.assertEqual(out["wlan0"], {"PCI Path": "PciRoot(0x0)/Pci(0x14,0x3)", "ACPI Path": "\\_SB.PC00.CNVW"})
        self.assertEqual(out["usb0"], {})
        self.assertIs(info.get_device_location_paths, located)   # the device/ fallback is only for the network step
        self.assertEqual(info.get_device_location_paths(wlan), {})

    def test_acpi_paths_outside_sb_are_dropped_like_windows(self):
        info = linuxhost.patch_sniffer(FakeInfo(locations={"/fan": {"ACPI Path": "\\_TZ_.FAN0"},
                                                           "/ec": {"ACPI Path": "\\_SB.PC00.LPCB.EC0_"}}))
        out = info.system_devices()
        self.assertEqual(out["Fan"], {"Bus Type": "ACPI"})
        self.assertEqual(out["EC"]["ACPI Path"], "\\_SB.PC00.LPCB.EC0_")

    def vmd_tree(self):
        """A VMD controller at 0000:00:0e.0 with an NVMe drive on the PCI domain it creates, as Linux lays it out."""
        devices = self.root / "devices"
        vmd = devices / "pci0000:00" / "0000:00:0e.0"
        nvme = vmd / "pci10000:e0" / "10000:e0:06.0" / "10000:e1:00.0"
        (nvme / "nvme" / "nvme0").mkdir(parents=True)
        (nvme / "nvme" / "nvme0" / "model").write_text("SAMSUNG MZVL21T0HCLR-00BTW        \n")
        sata = devices / "pci0000:00" / "0000:00:17.0"
        (sata / "ata1" / "host0" / "target0:0:0" / "0:0:0:0").mkdir(parents=True)
        (sata / "ata1" / "host0" / "target0:0:0" / "0:0:0:0" / "model").write_text("WDC WD10EZEX\n")
        bus = self.root / "bus"
        bus.mkdir()
        for slot, target in (("0000:00:0e.0", vmd), ("10000:e1:00.0", nvme), ("0000:00:17.0", sata)):
            os.symlink(target, bus / slot)
        return str(bus)

    def test_vmd_owns_the_drive_and_the_nvme_behind_it_is_not_listed(self):
        bus = self.vmd_tree()
        nvme_path = "PciRoot(0x10000)/Pci(0xe,0x0)/Pci(0x6,0x0)/Pci(0x0,0x0)"
        info = FakeInfo(devices_by_class={
            "RAID bus controller": [{"Name": "Intel Corporation Volume Management Device NVMe RAID Controller",
                                     "Device Path": "0000:00:0e.0", "Bus Type": "PCI", "Device ID": "8086-467F",
                                     "Subsystem ID": "00008086", "PCI Path": "PciRoot(0x0)/Pci(0xe,0x0)",
                                     "ACPI Path": "\\_SB.PC00.VMD0"}],
            "Non-Volatile memory controller": [{"Name": "Samsung NVMe", "Device Path": "10000:e1:00.0", "PCI Path": nvme_path}],
            "SATA controller": [{"Name": "Intel SATA", "Device Path": "0000:00:17.0", "PCI Path": "PciRoot(0x0)/Pci(0x17,0x0)"}]})
        info.storage_result = {"Samsung NVMe": {"Bus Type": "PCI", "Device ID": "144D-A80A", "PCI Path": nvme_path,
                                                "Disk Drives": ["SAMSUNG MZVL21T0HCLR-00BTW"]},
                               "Intel SATA": {"Bus Type": "PCI", "Device ID": "8086-51D3", "PCI Path": "PciRoot(0x0)/Pci(0x17,0x0)",
                                              "Disk Drives": ["WDC WD10EZEX"]}}
        linuxhost.patch_sniffer(info, pci_devices=bus)
        out = info.storage_controllers()
        self.assertEqual(list(out), ["Intel Corporation Volume Management Device NVMe RAID Controller", "Intel SATA"])
        vmd = out["Intel Corporation Volume Management Device NVMe RAID Controller"]
        self.assertEqual(vmd["Device ID"], "8086-467F")   # in OpCore-Simplify's IntelVMDIDs: the build stops with the BIOS fix
        self.assertEqual(vmd["Disk Drives"], ["SAMSUNG MZVL21T0HCLR-00BTW"])
        self.assertEqual(vmd["ACPI Path"], "\\_SB.PC00.VMD0")

    def test_without_raid_the_storage_section_is_unchanged(self):
        info = FakeInfo()
        info.storage_result = {"Samsung NVMe": {"Bus Type": "PCI", "Device ID": "144D-A80A"}}
        linuxhost.patch_sniffer(info)
        self.assertEqual(info.storage_controllers(), {"Samsung NVMe": {"Bus Type": "PCI", "Device ID": "144D-A80A"}})


@unittest.skipUnless(os.path.isdir(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "upstream",
                                                "Hardware-Sniffer", "Scripts", "platforms")), "upstream missing")
class UpstreamShape(unittest.TestCase):
    def test_the_wrapped_methods_exist_on_the_upstream_linux_collector(self):
        # patch_sniffer replaces these by name; if the pinned upstream renames one, this fails instead of the scan
        source = Path(__file__).resolve().parents[1] / "upstream" / "Hardware-Sniffer" / "Scripts" / "platforms" / "linux.py"
        text = source.read_text(encoding="utf-8")
        for name in ("def cpu(self)", "def network(self)", "def storage_controllers(self)", "self.get_device_location_paths =",
                     '"RAID bus controller"' if '"RAID bus controller"' in text else "devices_by_class"):
            self.assertIn(name, text)

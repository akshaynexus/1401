# 1401

Install macOS on the PC you already own: the reverse of Boot Camp. 1401 runs on Windows, checks your hardware, builds
an OpenCore setup for it and writes a macOS install stick. It is free and open source.

- Download: [releases page](https://github.com/nullmoth/1401/releases) (`1401-Windows-<version>.zip`)
- Linux: run `linux/install.sh` from this repository and open 1401 from the app menu ([docs/LINUX.md](docs/LINUX.md))
- Changes: [CHANGELOG.md](CHANGELOG.md)
- Problems: [troubleshooting](docs/TROUBLESHOOTING.md)
- How it works: [docs/HOW-IT-WORKS.md](docs/HOW-IT-WORKS.md)
- NVIDIA driver for macOS: [nvidia-macos-driver](https://github.com/nullmoth/nvidia-macos-driver)

> **This is [akshaynexus/1401](https://github.com/akshaynexus/1401), the owner's fork of nullmoth/1401** (branch
> `linux-port`: the Linux app, and the Acer Predator Helios 300 profile below). Changes here stay in this fork.
> nullmoth/1401 does not accept AI-written pull requests (AGENTS.md, CONTRIBUTING.md), so none are opened upstream.

1401 has been tested end to end on a small number of PCs, so expect problems on others. If it doesn't work on yours,
the [OpenCore Install Guide](https://dortania.github.io/OpenCore-Install-Guide/) covers setting OpenCore up by hand.

## Using it

1. Unzip the release and keep the `1401` folder together. Run `1401.exe` as administrator (it reads the hardware and
   writes the stick).
2. Click Check this PC, Build the Mac setup, read Your BIOS steps, then Create the macOS stick. You need a USB stick of
   4 GB or more that can be erased, and an internet connection.
3. Restart, change the BIOS settings 1401 showed you, boot the stick and pick the macOS installer in the OpenCore menu.
   In Disk Utility, erase only the partition or disk meant for macOS (APFS), never a disk with Windows or data you
   want to keep. Then choose Reinstall macOS; the rest of macOS downloads from Apple.
4. With an NVIDIA card (GTX 16 or newer): once macOS is running, open the `NullMoth` folder on the stick and run
   1401.app, or download `1401-Mac-<version>.dmg` from the driver's releases page. It installs the driver and updates
   the OpenCore settings on the stick. Keep the stick plugged in: OpenCore on it is what starts macOS.

5. Something went wrong? If the build fails, 1401 shows why and sends the log to nullmothsystems.com after telling you
   (Cancel keeps it on your PC). If macOS does not start from the stick, boot back into Windows, plug the stick in and open
   1401, or click **Scan for logs and send them**: the stick holds OpenCore's and macOS's startup logs and 1401 sends them.
   Quote the report ID it shows when you ask for help.

## What 1401 changes compared with the engine it builds on

1401 builds with [OpCore-Simplify](https://github.com/lzhoang2801/OpCore-Simplify) and then applies its own rules.
Every rule that fires is recorded in the build result.

| Rule | What it does |
|---|---|
| `sip-minimal` | SIP stays on unless a chosen feature needs it lowered, and then only as far as that feature needs |
| `tahoe-weg-amd`, `tahoe-weg-args` | macOS 26 with an AMD card: WhateverGreen removed (it panics on 26), its boot arguments stripped |
| `tahoe-intel-bt` | macOS 26 with Intel Bluetooth: `-ibtcompatbeta` |
| `arrow-lake-ctr`, `arrow-lake-ctrsmt` | Core Ultra 200 (Arrow Lake): CpuTopologyRebuild and `ctrsmt` off, matching a config that installs and runs macOS 15 on that CPU |
| `boot-logs-on-stick` | OpenCore writes its log, Apple's boot log and any macOS panic to the stick, so a PC that never reaches macOS can still report why |
| `keep-boot-args` | the stick never overwrites the boot arguments of a machine that already runs macOS |
| `nullmoth-boot-args`, `nullmoth-sip` | NVIDIA card in the driver's table: macOS 15, the driver's boot arguments, SIP `0x0A43` |
| `nullmoth-installer-bar` | NVIDIA card: small BAR for the installer (`ResizeAppleGpuBars 0`, `ResizeGpuBars -1`) so its screen survives PCI setup; the Mac app restores the full BAR after install |
| `helios-detected` | Acer Predator Helios 300 G3-571 (DMI product `Predator G3-571` + GTX 1060 Mobile `10de:1c20` with Acer subsystem `1025:118b`): the machine profile below applies |
| `helios-release`, `helios-kexts` | the pinned [macos_helios300](https://github.com/akshaynexus/macos_helios300) release manifest (SHA-256 pinned, every file checked): PG106, PG106FB, NMHDA, QCARomePatchRAM, Ath10kMac, AirportAth10k and two SSDTs, in the manifest's Kernel > Add / ACPI > Add order |
| `helios-keep-devices` | the GTX 1060, its HDMI audio, the QCA6174 Wi-Fi and the Bluetooth are not disabled (no `disable-gpu`, no _OFF SSDT), because the release drives them |
| `helios-device-properties`, `helios-nvram` | `hda-gfx` = `onboard-2` on the GPU and its HDMI audio; the two BlueToolFixup NVRAM keys |
| `helios-forbidden` | never for this machine: the Turing+ NVRM / NVRMFB / NVAccel / NVRMAGDC kexts, `-wegnoegpu`, `disable-gpu` on the GPU, the GPU-off SSDT |
| `helios-post-install` | every non-OpenCore file of the release (PG106FB, PG106Accel, the Metal plugin, the shader translator, NVK, the post-install scripts, `POST-INSTALL.md`, the licences) goes on the stick in `EFI/Helios300` with a README of the steps, their commands and every file there; OpenCore cannot inject them |
| `helios-oclp-post-install`, `helios-usb-map`, `helios-cpufriend`, `helios-release-pending` | build-result notes: OCLP root patches after install (AppleHDA, HD 630), the USB map, CPUFriend only if the SMBIOS is not MacBookPro14,3, and no release pinned yet |

## Acer Predator Helios 300 G3-571 (`p1401/helios300.py`)

The Helios's GTX 1060 Mobile (Pascal), QCA6174 Wi-Fi and Bluetooth have no driver in the engine or in the NullMoth
driver (Turing and newer). Their drivers come from the owner's
[macos_helios300](https://github.com/akshaynexus/macos_helios300) releases, and 1401 builds the laptop's macOS 26 EFI
from that release's `manifest.json` ([contract](https://github.com/akshaynexus/macos_helios300/blob/main/docs/MANIFEST.md)):

- **1401 builds nothing for it.** macos_helios300's own `tools/release.sh` builds every file and its manifest,
  and publishes them as a GitHub (pre-)release. 1401 downloads `manifest.json` from that release's assets and
  downloads every file it lists, then checks each SHA-256 (and every file inside a zipped bundle). It unpacks them
  into the EFI in the manifest's Kernel > Add order and applies the manifest's SSDTs, DeviceProperties, NVRAM and
  boot-args. No SDK, toolchain or cross-build is involved.
- **Which release**: the tag + manifest SHA-256 pinned in `p1401/helios300_release.json`, or the latest release
  (pre-releases included) while nothing is pinned, or `--release vX.Y.Z|latest`. Files are cached next to the
  engine's download cache (`helios300-release/<tag>/`), so a rebuild works offline. A file that fails a check stops
  the build. With no release published and none cached, the Helios builds on its HD 630 with the laptop basics, and
  the build result says the GPU, Wi-Fi and Bluetooth drivers were not added.
- **Local release**: `--release-dir DIR` (a release directory such as macos_helios300's `dist/`) or `--manifest PATH`,
  for offline builds or before a release exists. It is recorded as *LOCAL, UNPINNED* in the build result.
- **Build from source (optional)**: `--build-from-source [--ref <tag|commit|branch>]` clones macos_helios300 at that
  ref with its submodules and runs *its* `tools/release.sh`. The `dist/manifest.json` it writes then goes through
  exactly the same checks and assembly. It needs macos_helios300's Linux cross toolchain (macOS 26 SDK, Apple ld64,
  ...); without it the build stops and names the docs to follow (macos_helios300 `docs/MANIFEST.md`).
- **macOS 26 only** (the release's MinKernel 25.0.0). A macOS 15 build gets the laptop basics without the release.
- **Laptop basics** on top of what OpCore-Simplify picks (VirtualSMC + SMCBatteryManager + SMCProcessor, ECEnabler,
  WhateverGreen with the Kaby Lake laptop platform-id `0x591B0000`, RealtekRTL8111, USBToolBox, BrightnessKeys,
  RestrictEvents, Lilu; SSDT-EC/USBX/PLUG/PNLF/XOSI/GPI0): AppleALC with **layout-id 29** (AppleALC's own ALC255
  layout "dhinakg - Realtek ALC255 for Acer Predator G3-571") instead of VoodooHDA, because NMHDA's HDMI audio
  goes through AppleALC; VoodooPS2Controller, VoodooI2C + VoodooI2CHID (SYNA7DB5 touchpad), RealtekCardReader +
  RealtekCardReaderFriend for the RTL8411B, and BlueToolFixup (BrcmPatchRAM ≥ 2.7.2) for QCARomePatchRAM. A Linux
  scan names input devices by `Device ID`, which the engine does not read, so it misses the input kexts otherwise.
- **SMBIOS MacBookPro14,3**, the engine's pick: the 2017 15-inch with the same i7-7700HQ and HD 630, so no CPUFriend
  data is needed. RestrictEvents' `revpatch=sbvmm` (`tahoe-sbvmm`) lets it run macOS 26.
- **After installing macOS 26**: OpenCore Legacy Patcher root patches for AppleHDA (removed in macOS 26; AppleALC and
  NMHDA need it) and the Kaby Lake graphics of the HD 630 (macos_helios300 `docs/HARDWARE.md`). The build lowers
  SIP for them, and the result lists the step. Then install `EFI/Helios300` from the stick (README there).
- The stick is written by the usual writer (`p1401.usbwriter write`); the post-install kit is part of the EFI it copies.

```bash
python3 -m p1401 build Report.json ACPI out --macos 25                         # the published release (default)
python3 -m p1401 build Report.json ACPI out --macos 25 --release-dir ~/macos_helios300/dist   # a local one
python3 -m p1401 build Report.json ACPI out --macos 25 --build-from-source --ref v0.1.0      # build it yourself
```

## Tests

```bash
python3 tests/corpus/fetch.py          # public Hardware Sniffer reports, pinned by commit
python3 tests/run_corpus.py build      # build + rules + ocvalidate for every machine in the corpus
python3 -m unittest tests.test_helios300   # Helios profile; A1401_HELIOS_ACPI=<ACPI dump> adds the full build + ocvalidate
python3 -m p1401.selftest && python3 -m p1401.guide --selftest && python3 -m p1401.apple --selftest \
  && python3 -m p1401.usbwriter --selftest && python3 -m p1401.scan --selftest && python3 -m p1401.nullmoth --selftest
```

`loader/` is an experimental UEFI boot layer (Rust) tested in QEMU only. The app does not use it; sticks boot with
OpenCore.

## Legal

1401 is free of charge, forever, under the **PolyForm Noncommercial License 1.0.0** (`LICENSE`): use it, change it,
share it, but nobody may sell it or use it to make money. Third-party parts keep their own licences (`NOTICE.md`).
The NullMoth name and the moth logo belong to NullMoth Systems and are not licensed.

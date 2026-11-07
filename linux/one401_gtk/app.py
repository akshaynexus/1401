"""The 1401 window for GNOME (GTK 4 + libadwaita): windows/App/MainForm.cs, step for step.

Six steps in the same order (Introduction, Check this PC, Build the Mac setup, Your BIOS steps, Create the macOS stick,
Done), the same gating between them, the same confirmations before a disk is erased and before a log leaves the PC,
and the same engine calls. Only how root is obtained differs: the scan and the write go through pkexec."""
import json
import os
import re
import sys
import traceback
import uuid
import webbrowser

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk, Pango  # noqa: E402

try:
    gi.require_version("WebKit", "6.0")
    from gi.repository import WebKit  # noqa: E402
except (ValueError, ImportError):
    WebKit = None

from . import engine, hwmap, logic, reports, sysdisks, upload  # noqa: E402
from .aio import Future, in_thread, resolved, spawn  # noqa: E402
from .loc import F, Op, T  # noqa: E402

APP_ID = "com.nullmoth.A1401"
STEPS = ["Introduction", "Check this PC", "Build the Mac setup", "Your BIOS steps", "Create the macOS stick", "Done"]
OPENCORE_GUIDE = "https://dortania.github.io/OpenCore-Install-Guide/"
RELEASES = "https://github.com/nullmoth/1401/releases"
SETTINGS = os.path.join(engine.WORK, "linux-settings.json")

CSS = b"""
.a1401-sidebar { padding: 16px 8px 8px 12px; }
.a1401-step { padding: 9px 12px; border-radius: 9px; }
.a1401-step.current { font-weight: bold; color: @accent_color; background: alpha(@accent_bg_color, 0.18);
                      box-shadow: inset 0 0 0 1px alpha(@accent_color, 0.6); }
.a1401-step.done { color: @success_color; }
.a1401-step.todo { opacity: 0.6; }
.a1401-log { font-family: monospace; font-size: 0.92em; padding: 6px; }
.a1401-link { padding: 2px 4px; min-height: 0; }
.a1401-link label { color: @accent_color; text-decoration: underline; }
"""


def _label(text="", wrap=True, css=None, xalign=0.0, selectable=False):
    lab = Gtk.Label(label=text, xalign=xalign, wrap=wrap, selectable=selectable)
    if wrap:
        lab.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    for c in css or []:
        lab.add_css_class(c)
    return lab


def _open_uri(uri):
    try:
        Gio.AppInfo.launch_default_for_uri(uri, None)
    except GLib.Error:
        webbrowser.open(uri)


class Window(Adw.ApplicationWindow):
    def __init__(self, app, dry_run=False):
        super().__init__(application=app, title=T("1401 Assistant"))
        self.dry_run = dry_run
        self.version = engine.version()
        self.sender = upload.Sender(self.version)
        self.page = 0
        self.busy = self.scanned = self.built = self.written = self.listing = self.showing_prerequisites = False
        self.must_update = None
        self.build_log_files = set()
        self.stick_roots = []
        self.prerequisite_return_page = 1
        self.firmware_guide_file = None
        self.scan_dir = self.scan_run_id = self.efi_dir = self.guide_file = None
        self.darwin, self.macos_full, self.summary = "24", "24.99.99", ""
        self.last = None            # the last engine Result (its .report is Engine.LastReport)
        self.disk_items = []
        self.set_default_size(960, 680)
        self.set_size_request(640, 420)
        self.set_icon_name(APP_ID)
        self._build_ui()
        self.connect("close-request", self._on_close_request)
        self.go(0)

    # --- construction -------------------------------------------------------------------------------------------------

    def _link(self, text, handler):
        b = Gtk.Button()
        b.set_child(_label(text))
        b.add_css_class("flat")
        b.add_css_class("a1401-link")
        b.set_halign(Gtk.Align.START)
        b.connect("clicked", lambda *_: handler())
        return b

    def _build_ui(self):
        provider = Gtk.CssProvider()
        provider.load_from_string(CSS.decode())
        Gtk.StyleContext.add_provider_for_display(self.get_display(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self._load_theme()

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.head_title = Adw.WindowTitle(title="1401 — " + T(STEPS[0]))
        header.set_title_widget(self.head_title)
        theme = Gtk.Button(icon_name="weather-clear-night-symbolic", tooltip_text=T("Light or dark"))
        theme.connect("clicked", lambda *_: self._toggle_theme())
        header.pack_end(theme)
        toolbar.add_top_bar(header)

        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        nav = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, width_request=220)
        nav.add_css_class("a1401-sidebar")
        self.side = []
        for i, s in enumerate(STEPS):
            lab = _label(f"{i + 1}.  {T(s)}", css=["a1401-step"])
            nav.append(lab)
            self.side.append(lab)
        outer.append(nav)
        outer.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=18, margin_bottom=12,
                       margin_start=24, margin_end=24, hexpand=True, vexpand=True)
        self.title_label = _label(css=["title-1"], wrap=True)
        self.note = _label()
        body.append(self.title_label)
        body.append(self.note)

        self.facts = Gtk.Grid(column_spacing=24, row_spacing=4)
        self.facts_frame = Gtk.Frame(child=self.facts)
        self.facts.set_margin_top(8); self.facts.set_margin_bottom(8)
        self.facts.set_margin_start(12); self.facts.set_margin_end(12)
        body.append(self.facts_frame)

        self.disk_row = Gtk.Box(spacing=12)
        self.disk_list = Gtk.StringList()
        self.disks = Gtk.DropDown(model=self.disk_list, hexpand=True)
        self.refresh = Gtk.Button(label=T("Refresh"))
        self.refresh.connect("clicked", lambda *_: self.fill_disks())
        self.disk_row.append(self.disks)
        self.disk_row.append(self.refresh)
        body.append(self.disk_row)

        self.arm_apps = Gtk.CheckButton()
        self.arm_apps.set_child(_label(T("Also set up ARM apps (opt-in): after macOS starts, 1401 offers to finish setting "
                                         "up ARM app support with one click, if this Mac's processor can run it.")))
        body.append(self.arm_apps)

        self.bar = Gtk.ProgressBar(show_text=False)
        body.append(self.bar)

        self.os_pick = Gtk.DropDown.new_from_strings([T("macOS 15 Sequoia"), T("macOS 26 Tahoe")])
        self.os_pick.set_halign(Gtk.Align.START)
        self.os_pick.set_size_request(300, -1)
        self.os_pick.connect("notify::selected", lambda *_: self._choice_changed())
        body.append(self.os_pick)

        self.exp_drivers = Gtk.CheckButton()
        self.exp_drivers.set_child(_label(T("Include experimental drivers (Wi-Fi, AMD RDNA 4 graphics, USB and power fixes)")))
        self.exp_drivers.connect("toggled", lambda *_: self._choice_changed())
        body.append(self.exp_drivers)

        if WebKit is not None:
            self.web = WebKit.WebView(vexpand=True, hexpand=True)
            self.web.set_size_request(-1, 180)
            self.guide = Gtk.Frame(child=self.web, vexpand=True)
        else:
            self.web = None
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, vexpand=True)
            box.append(_label(T("This page opens in your web browser (WebKitGTK is not installed).")))
            self.guide_open = Gtk.Button(label=T("Open the page"), halign=Gtk.Align.START)
            self.guide_open.connect("clicked", lambda *_: self._open_guide_externally())
            box.append(self.guide_open)
            self.guide = box
        self.guide_path = None
        body.append(self.guide)

        self.log_view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.log_view.add_css_class("a1401-log")
        self.log_buffer = self.log_view.get_buffer()
        self.log = Gtk.ScrolledWindow(child=self.log_view, vexpand=True, min_content_height=120)
        self.log.set_has_frame(True)
        body.append(self.log)

        self.oc_link = self._link(T("OpenCore Install Guide (dortania.github.io/OpenCore-Install-Guide)"),
                                  lambda: _open_uri(OPENCORE_GUIDE))
        self.log_link = self._link(T("Something went wrong? Scan for logs and send them"), lambda: self.spawn(self.offer_stick_logs(True)))
        self.coffee_link = self._link(T("Buy me a coffee"), lambda: _open_uri("https://buymeacoffee.com/nullmoth"))
        self.credits_link = self._link(T("Credits"), self._credits)
        self.drv_link = self._link(T("Update the NVIDIA driver (newest release, here and on your 1401 stick)"),
                                   lambda: self.spawn(self.update_driver()))
        self.app_link = self._link(T("Update 1401 (this app)"), lambda: self.spawn(self.update_app()))
        self.doctor_link = self._link(T("Built your own EFI? Check it (and fix it) with the same rules 1401 uses"),
                                      lambda: self.spawn(self.check_own_efi()))
        self.firmware_link = self._link(T("Review firmware prerequisites"), self._firmware_link_clicked)
        for w in (self.oc_link, self.log_link, self.coffee_link, self.credits_link, self.drv_link, self.app_link,
                  self.doctor_link, self.firmware_link):
            body.append(w)

        scroller = Gtk.ScrolledWindow(child=body, hscrollbar_policy=Gtk.PolicyType.NEVER, hexpand=True, vexpand=True)
        scroller.set_propagate_natural_height(False)
        self.scroller = scroller
        outer.append(scroller)
        toolbar.set_content(outer)

        foot = Gtk.ActionBar()
        self.next = Gtk.Button(label=T("Continue >"))
        self.next.add_css_class("suggested-action")
        self.next.connect("clicked", lambda *_: self.spawn(self.on_next()))
        self.back = Gtk.Button(label=T("< Back"))
        self.back.connect("clicked", lambda *_: self._on_back())
        foot.pack_end(self.next)
        foot.pack_end(self.back)
        toolbar.add_bottom_bar(foot)
        self.set_content(toolbar)

    # --- theme --------------------------------------------------------------------------------------------------------

    def _load_theme(self):
        try:
            with open(SETTINGS, encoding="utf-8") as fh:
                theme = json.load(fh).get("theme")
        except (OSError, ValueError, AttributeError):
            theme = None
        scheme = {"light": Adw.ColorScheme.FORCE_LIGHT, "dark": Adw.ColorScheme.FORCE_DARK}.get(theme, Adw.ColorScheme.DEFAULT)
        Adw.StyleManager.get_default().set_color_scheme(scheme)

    def _toggle_theme(self):
        sm = Adw.StyleManager.get_default()
        dark = not sm.get_dark()
        sm.set_color_scheme(Adw.ColorScheme.FORCE_DARK if dark else Adw.ColorScheme.FORCE_LIGHT)
        try:
            os.makedirs(engine.WORK, exist_ok=True)
            with open(SETTINGS, "w", encoding="utf-8") as fh:
                json.dump({"theme": "dark" if dark else "light"}, fh)
        except OSError:
            pass

    # --- small helpers ------------------------------------------------------------------------------------------------

    def spawn(self, coro):
        return spawn(coro, self._failed)

    def _failed(self, error):
        text = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        sys.stderr.write(text)
        report = reports.save(engine.WORK, "crash", self.version, "Linux window\n\n" + text)
        self.say(reports.clean(str(error)))
        self.say(T("A local crash report was saved at ") + report.path + T(". Scan for logs and send them can retry after restart.")
                 if report.saved else report.failure)
        self.busy = False
        self.next.set_sensitive(True)
        self.refresh.set_sensitive(True)

    def say(self, s):
        """MainForm.Say: PROGRESS lines move the bar, every other line goes to the log."""
        p = logic.progress(s)
        if p is not None:
            if p >= 0:
                self.bar.set_fraction(p / 100)
            return False
        end = self.log_buffer.get_end_iter()
        self.log_buffer.insert(end, s + "\n")
        mark = self.log_buffer.create_mark(None, self.log_buffer.get_end_iter(), False)
        self.log_view.scroll_mark_onscreen(mark)
        self.log_buffer.delete_mark(mark)
        return False

    def say_threadsafe(self, s):
        GLib.idle_add(self.say, s)

    def clear_log(self):
        self.log_buffer.set_text("")

    def run(self, args, privileged=False, show=True):
        """Engine.Run as an awaitable Result; with show, every line goes to the log as it arrives."""
        fut = Future()
        if self.dry_run:
            self.say("[dry run] " + " ".join(args))
            fut.resolve(engine.Result(1, [], None))
            return fut
        engine.run_in_thread(args, self.say_threadsafe if show else (lambda _l: None),
                             lambda result: GLib.idle_add(self._finished, fut, result), privileged)
        return fut

    def _finished(self, fut, result):
        self.last = result
        fut.resolve(result)
        return False

    def alert(self, heading, body, responses, default=None, close=None, destructive=(), extra=None):
        """An Adw.AlertDialog; resolves to the response id."""
        fut = Future()
        d = Adw.AlertDialog(heading=heading, body=body)
        for rid, label in responses:
            d.add_response(rid, label)
        for rid in destructive:
            d.set_response_appearance(rid, Adw.ResponseAppearance.DESTRUCTIVE)
        if default:
            d.set_default_response(default)
        if close:
            d.set_close_response(close)
        if extra is not None:
            d.set_extra_child(extra)
        d.connect("response", lambda _d, r: fut.resolve(r))
        d.present(self)
        self._dialog = d
        return fut

    def message(self, text, heading="1401"):
        return self.alert(heading, text, [("ok", T("OK"))], default="ok", close="ok")

    def ok_cancel(self, text, heading, ok=None):
        return self.alert(heading, text, [("cancel", T("Cancel")), ("ok", ok or T("OK"))], default="ok", close="cancel")

    def report_save_status(self):
        r = self.last.report if self.last else None
        if r is not None and not r.saved:
            self.say(r.failure + T(" The operation output may remain visible, but a durable final report is unavailable."))

    def show(self, *on):
        """MainForm.Show."""
        for w in (self.facts_frame, self.bar, self.log, self.disk_row, self.os_pick, self.exp_drivers, self.guide,
                  self.oc_link, self.log_link, self.arm_apps):
            w.set_visible(w in on)
        if self.facts_frame.get_visible() and self.facts.get_first_child() is None:
            self.facts_frame.set_visible(False)
        for w in (self.drv_link, self.app_link, self.coffee_link, self.credits_link, self.doctor_link):
            w.set_visible(self.log_link.get_visible())
        self.firmware_link.set_visible(self.page in (1, 2) and self.firmware_guide_file is not None
                                       and os.path.isfile(self.firmware_guide_file))
        self.firmware_link.set_sensitive(not self.busy)

    def _navigate_guide(self, path):
        self.guide_path = path
        if self.web is not None:
            self.web.load_uri(GLib.filename_to_uri(path, None))

    def _open_guide_externally(self):
        if self.guide_path and os.path.isfile(self.guide_path):
            _open_uri(GLib.filename_to_uri(self.guide_path, None))

    def _credits(self):
        notice = os.path.join(engine.HOME, "NOTICE.md")
        _open_uri(GLib.filename_to_uri(notice, None) if os.path.isfile(notice) else "https://github.com/nullmoth/1401/blob/main/NOTICE.md")

    def _choice_changed(self):
        # a different macOS or driver choice needs different startup files: drop the finished build so Build runs again
        if self.built and not self.busy:
            self.built = False
            self.summary = ""
            self.go(2)

    def _firmware_link_clicked(self):
        if self.busy or self.firmware_guide_file is None or not os.path.isfile(self.firmware_guide_file):
            return
        self.prerequisite_return_page = self.page
        self.showing_prerequisites = True
        self.go(3)

    def _on_back(self):
        if self.page == 3 and self.showing_prerequisites:
            self.showing_prerequisites = False
            self.go(self.prerequisite_return_page)
        else:
            self.go(self.page - 1)

    def _on_close_request(self, *_):
        if not self.busy or getattr(self, "_closing", False):
            return False
        async def ask():
            r = await self.alert(T("1401 is still working"), T("Closing 1401 now stops the step that is running. A stick "
                                 "that is being written may be left half-written."),
                                 [("stay", T("Keep 1401 open")), ("close", T("Close anyway"))], default="stay",
                                 close="stay", destructive=("close",))
            if r == "close":
                self._closing = True
                self.close()
        self.spawn(ask())
        return True

    # --- pages --------------------------------------------------------------------------------------------------------

    def go(self, p):
        """MainForm.Go."""
        if self.busy or p < 0 or p >= len(STEPS):
            return
        self.page = p
        self.scroller.get_vadjustment().set_value(0)
        self.head_title.set_title("1401 — " + T(STEPS[p]))
        for i, lab in enumerate(self.side):
            for c in ("current", "done", "todo"):
                lab.remove_css_class(c)
            lab.add_css_class("current" if i == p else ("done" if i < p else "todo"))
        self.title_label.set_label(T(STEPS[p]))
        self.back.set_sensitive(0 < p < len(STEPS) - 1)
        self.next.set_label(T("Continue >"))
        if p == 0:
            self.note.set_label(T(
                "The 1401 Assistant helps you install macOS on this PC, the way Boot Camp put Windows on a Mac.\r\n\r\n"
                "It checks this PC, builds the startup files macOS needs for your exact hardware, shows BIOS settings based on your scan and startup files, "
                "and makes a macOS install stick. Windows stays as it is. Nothing about this PC is sent anywhere.\r\n\r\n"
                "You need: an internet connection and a USB stick of 4 GB or more that can be erased.\r\n\r\n"
                "1401 is new and may not work on every PC. If it does not work on yours, set up OpenCore by hand with the guide below; "
                "the NullMoth app works on any OpenCore setup."))
            self.show(self.oc_link, self.log_link)
            miss = engine.missing()
            if miss is not None:
                self.note.set_label(miss)
                self.next.set_sensitive(False)
            else:
                note = engine.root_note()
                if note:
                    self.note.set_label(self.note.get_label() + "\n\n" + note)
                self.next.set_sensitive(True)
        elif p == 1:
            self.note.set_label(T("This PC was checked. Continue, or check again." if self.scanned else
                                  "1401 reads this PC's hardware (processor, board, graphics, network, storage) and its ACPI tables. This takes about a minute."))
            self.next.set_label(T("Continue >" if self.scanned else "Check this PC"))
            self.show(self.facts_frame, self.log)
            self.next.set_sensitive(True)
        elif p == 2:
            self.note.set_label(self.summary.split("\n")[0] if self.built else T(
                "Pick macOS 15 Sequoia or macOS 26 Tahoe below. 1401 builds the startup files (OpenCore EFI) for that version and checks them with OpenCore's own validator. Downloads OpenCore and drivers."))
            self.next.set_label(T("Continue >" if self.built else "Build"))
            self.show(self.facts_frame, self.log, self.os_pick, self.exp_drivers)
            self.next.set_sensitive(self.scanned)
            if self.built:
                self.log_buffer.set_text(self.summary)
                self.log_view.scroll_to_iter(self.log_buffer.get_start_iter(), 0, False, 0, 0)
        elif p == 3:
            if self.showing_prerequisites:
                self.title_label.set_label(T("Firmware prerequisites"))
                self.note.set_label(T("Review storage requirements before building. No EFI or firmware changes are made by this page. Return to the check or build when finished."))
                self.show(self.guide)
                self.next.set_sensitive(True)
                self.next.set_label(T("Return >"))
                if self.firmware_guide_file and os.path.isfile(self.firmware_guide_file):
                    self._navigate_guide(self.firmware_guide_file)
            else:
                self.note.set_label(T("These BIOS suggestions follow your scan and startup files. Check menu names in the exact board manual, then take a photo before restarting."))
                self.show(self.guide)
                self.next.set_sensitive(self.built)
                if self.guide_file and os.path.isfile(self.guide_file):
                    self._navigate_guide(self.guide_file)
        elif p == 4:
            nvidia = "NVIDIA" in self.summary
            self.note.set_label(T("Plug in the USB stick that will become the macOS installer. EVERYTHING on it is erased.\r\n"
                                  "1401 downloads macOS from Apple onto it and adds the startup files")
                                + T(" and the NullMoth NVIDIA driver." if nvidia else "."))
            self.next.set_label(T("Continue >" if self.written else "Erase and create"))
            # the ARM apps request is acted on after the NVIDIA driver is set up, so only a stick that carries it offers it
            if nvidia:
                self.show(self.disk_row, self.arm_apps, self.bar, self.log)
            else:
                self.show(self.disk_row, self.bar, self.log)
            self.arm_apps.set_sensitive(not self.written)
            self.next.set_sensitive(True)
            self.fill_disks()
        elif p == 5:
            self.note.set_label(T("The macOS stick is ready.\r\n\r\n1. Restart and open your board's boot menu (the key is in your BIOS steps), then choose the stick.\r\n"
                                  "2. In the 1401 boot menu choose \"Install macOS\" and follow Apple's installer. The screen can stay still for up to two minutes while macOS is read from the stick: do not restart.\r\n"
                                  "3. When macOS is running, open the NullMoth folder on the stick and run 1401.app to finish the NVIDIA driver."))
            self.next.set_label(T("Close"))
            self.show()
            self.next.set_sensitive(True)

    # --- the steps ----------------------------------------------------------------------------------------------------

    async def prepare_firmware_guide(self):
        self.firmware_guide_file = None
        if not self.scanned or self.scan_dir is None or self.scan_run_id is None:
            return
        target = os.path.join(engine.WORK, f"Firmware-prerequisites-{self.scan_run_id}.html")
        self.busy = True
        self.next.set_sensitive(False)
        try:
            res = await self.run(logic.firmware_guide_args(os.path.join(self.scan_dir, "Report.json"), self.scan_run_id, target))
            self.report_save_status()
            if res.rc == 0 and os.path.isfile(target):
                self.firmware_guide_file = target
            else:
                self.say(T("Firmware review could not be generated. No EFI or firmware was changed. The build refusal still includes storage preparation guidance."))
        except Exception:  # noqa: BLE001
            self.say(T("Firmware review is unavailable. No EFI or firmware was changed; use the storage guidance in the build refusal."))
        finally:
            self.busy = False
            self.next.set_sensitive(True)

    async def on_next(self):
        """MainForm.OnNext."""
        if self.busy:
            return
        if self.must_update is not None:
            if not await self.ask_to_update():
                return
        if self.page == 3 and self.showing_prerequisites:
            self.showing_prerequisites = False
            self.go(self.prerequisite_return_page)
            return
        if self.page == 0:
            self.go(1)
            return
        # The PC restarted since Check this PC ran (a firmware change needs one): that report is history.
        rechecking = self.page == 1 and self.scanned and reports.older_than_this_boot(self.scan_dir)
        if rechecking:
            self.scanned = self.built = False
        if self.page == 1 and not self.scanned:
            await self.check_this_pc(rechecking)
            return
        if self.page == 2 and not self.built:
            await self.build()
            return
        if self.page == 4 and not self.built:
            self.go(2)
            return
        if self.page == 4 and not self.written:
            await self.write_stick()
            return
        if self.page == 5:
            self.close()
            return
        self.go(self.page + 1)

    async def check_this_pc(self, rechecking):
        self.busy = True
        self.next.set_sensitive(False)
        self.clear_log()
        if rechecking:
            self.say(T("This PC restarted since it was checked; checking it again."))
        self.scan_run_id = uuid.uuid4().hex
        self.scan_dir = reports.run_directory(engine.WORK, self.scan_run_id)
        try:
            os.makedirs(self.scan_dir, mode=0o700, exist_ok=True)
            reports.remember(engine.WORK, self.scan_run_id)
        except OSError as error:
            self.busy = False
            self.next.set_sensitive(True)
            self.say(T("Could not create the working folder: ") + str(error))
            return
        self.say(T("Checking this PC..."))
        res = await self.run(logic.scan_args(self.scan_dir, self.scan_run_id), privileged=True)
        self.report_save_status()
        self.busy = False
        self.next.set_sensitive(True)
        if res.rc != 0:
            self.say(T("The check failed. The lines above say why."))
            if not res.refused:
                await self.offer_operation_report("check")
            return
        # the hardware map rides along with the scan it belongs to, beside Report.json; it never fails the check
        self.say(await in_thread(hwmap.write_to, self.scan_dir))
        self.scanned = True
        self.load_facts()
        await self.prepare_firmware_guide()
        self.say(T("Done."))
        self.go(1)

    async def build(self):
        self.busy = True
        self.next.set_sensitive(False)
        self.clear_log()
        self.efi_dir = os.path.join(engine.WORK, "efi")
        rep = os.path.join(self.scan_dir, "Report.json")
        acpi = os.path.join(self.scan_dir, "ACPI")
        self.say(T("Building the startup files for this PC..."))
        # A stick that already failed to boot says why in its newest OpenCore log; the build reads it (bootfix.py).
        roots = list(self.stick_roots) + await in_thread(sysdisks.removable_roots)
        log_files = logic.stick_boot_logs(roots)
        self.build_log_files = set(log_files)
        stopped = ""
        try:
            with open(os.path.join(engine.WORK, "stopped-at.txt"), encoding="utf-8") as fh:
                stopped = fh.read().strip()
        except OSError:
            pass
        if log_files or stopped:
            self.say(T("Using the last startup log on the stick to adjust this build."))
        res = await self.run(logic.build_args(rep, acpi, self.efi_dir, self.os_pick.get_selected() == 1,
                                              self.exp_drivers.get_active(), log_files, stopped), show=False)
        self.report_save_status()
        self.busy = False
        self.next.set_sensitive(True)
        try:
            out = logic.read_build(res.rc, res.lines)
        except Exception as e:  # noqa: BLE001
            self.say(F("Could not read the build result ({0}). The saved operation report includes the engine output.", type(e).__name__))
            for l in res.lines[:40]:
                self.say(l)
            await self.offer_operation_report("build")
            return
        if not out.ok:
            self.say(out.message)
            # the whole engine output, so a user can post it in a bug report
            await self.offer_operation_report("build")
            return
        self.macos_full, self.darwin, self.summary = out.macos_full, out.darwin, out.summary
        self.built = True
        self.showing_prerequisites = False
        self.guide_file = os.path.join(engine.WORK, "BIOS-steps.html")
        await self.run(logic.guide_args(rep, self.efi_dir, self.macos_full, self.guide_file))
        self.report_save_status()
        self.say("Done.")
        self.go(2)

    async def write_stick(self):
        i = self.disks.get_selected()
        d = self.disk_items[i] if 0 <= i < len(self.disk_items) else None
        if d is None:
            await self.message(T("Plug in a USB stick and press Refresh."))
            return
        # The build reads a failed start's log only from a stick that was plugged in while it ran; erasing deletes it.
        picked = list(d.mounts)
        if any(f not in self.build_log_files for f in logic.stick_boot_logs(picked)):
            self.stick_roots = picked
            self.say(T("This stick holds the log of a start that did not finish. Building again with it before the stick is erased."))
            self.built = False
            self.go(2)
            await self.on_next()
            return
        if await self.confirm_erase(d) != "erase":
            return
        self.busy = True
        self.next.set_sensitive(False)
        self.refresh.set_sensitive(False)
        self.clear_log()
        self.bar.set_fraction(0)
        dirs = engine.nullmoth_dirs()
        report = os.path.join(self.scan_dir, "Report.json")
        args = logic.write_args(d, self.efi_dir, self.darwin,
                                driver=logic.companion(dirs, "nullmoth-nvidia-", ".tar.gz", self.version),
                                extra=logic.companion(dirs, "1401-Mac-", ".zip", self.version),
                                profile=report if os.path.isfile(report) else None,
                                arm_apps=self.arm_apps.get_active() and self.arm_apps.get_visible())
        res = await self.run(args, privileged=True)
        lines = list(res.lines)
        # The stick holds a Mac or Windows partition: name what is there and ask once more; only an explicit yes erases.
        found = logic.os_partitions(lines)
        if res.rc != 0 and found is not None:
            again = await self.confirm_os_partitions(found)
            if again == "erase":
                res = await self.run(args + ["--erase-os-partitions"], privileged=True)
                lines += res.lines
        self.report_save_status()
        self.busy = False
        self.next.set_sensitive(True)
        self.refresh.set_sensitive(True)
        # a stale disk number makes every retry fail the same way: reload the list
        if res.rc != 0 and logic.disk_renumbered(lines):
            self.fill_disks()
        if res.rc != 0 and logic.scan_changed(lines):
            self.say(T("The startup files were built from an earlier check of this PC. Building them again for this check."))
            self.built = False
            self.go(2)
            await self.on_next()
            return
        if res.rc != 0:
            if res.refused:
                return
            self.say(T("Writing the stick did not finish. The saved report includes the writer output; partial changes to the selected stick may remain."))
            await self.offer_operation_report("write")
            return
        self.written = True
        self.bar.set_fraction(1.0)
        self.go(5)

    def _confirm_box(self, text):
        """The explicit acknowledgement an erase needs: the Erase button stays off until it is ticked."""
        box = Gtk.CheckButton()
        box.set_child(_label(text))
        return box

    def confirm_erase(self, d):
        detail = (F("Erase {0}", d) + F(" and make it the macOS installer?\r\n\r\nEverything on it will be lost.", d))
        if d.device:
            detail += "\n\n" + T("Device: ") + d.device + "  ·  " + logic.gb(d.size) + " GB"
        check = self._confirm_box(T("I understand that everything on this stick will be erased."))
        fut = self.alert(T("1401 - erase this stick?"), detail, [("cancel", T("Cancel")), ("erase", T("Erase"))],
                         default="cancel", close="cancel", destructive=("erase",), extra=check)
        dialog = self._dialog
        dialog.set_response_enabled("erase", False)
        check.connect("toggled", lambda b: dialog.set_response_enabled("erase", b.get_active()))
        return fut

    def confirm_os_partitions(self, what):
        text = ("This stick is not empty: " + what + "\r\n\r\nThat is usually an old macOS installer, but it can also be a "
                "Mac backup or a Mac or Windows system. Erase it anyway?").replace("\r\n", "\n")
        check = self._confirm_box(T("I understand that what is on this stick will be erased."))
        fut = self.alert("1401 - erase what is on this stick?", text, [("cancel", T("Cancel")), ("erase", T("Erase"))],
                         default="cancel", close="cancel", destructive=("erase",), extra=check)
        dialog = self._dialog
        dialog.set_response_enabled("erase", False)
        check.connect("toggled", lambda b: dialog.set_response_enabled("erase", b.get_active()))
        return fut

    def load_facts(self):
        child = self.facts.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.facts.remove(child)
            child = nxt
        try:
            with open(os.path.join(self.scan_dir, "Report.json"), encoding="utf-8") as fh:
                rows = logic.facts(json.load(fh))
            for i, (k, v) in enumerate(rows):
                key = _label(k, wrap=False)
                key.add_css_class("dim-label")
                self.facts.attach(key, 0, i, 1, 1)
                self.facts.attach(_label(v, selectable=True), 1, i, 1, 1)
        except Exception as e:  # noqa: BLE001
            self.say(T("Could not read the report: ") + str(e))

    # --- USB sticks ---------------------------------------------------------------------------------------------------

    def fill_disks(self):
        self.spawn(self._fill_disks())

    async def _fill_disks(self):
        """MainForm.FillDisks: the writer's own list (so the number, id and size are the ones the write re-checks),
        with the same filters, on a worker with a 30 s bound."""
        if self.listing:
            return
        self.listing = True
        self.disk_items = []
        self.disk_list.splice(0, self.disk_list.get_n_items(), [])
        self.refresh.set_sensitive(False)
        next_was = self.next.get_sensitive()
        self.next.set_sensitive(False)
        self.say(T("Looking for USB sticks..."))
        err, offered, skipped = await in_thread(self._list_sticks)
        if err:
            self.say(err)
        self.disk_items = offered
        self.disk_list.splice(0, 0, [str(d) for d in offered])
        if offered:
            self.disks.set_selected(0)
            self.say(F("Found {0} USB stick(s).", len(offered)))
        elif not err:
            self.say(T("No USB stick found. Plug one in (4 GB or more, not the stick 1401 runs from) and press Refresh."))
            if skipped:
                self.say(T("Disks 1401 saw and left out:"))
                for w in skipped:
                    self.say("  " + w)
        self.listing = False
        self.refresh.set_sensitive(not self.busy)
        if self.page == 4:
            self.next.set_sensitive(next_was and not self.busy)

    def _list_sticks(self):
        if self.dry_run:
            raw = [{"Number": 1, "FriendlyName": "Example USB stick", "UniqueId": "dry-run", "BusType": "USB",
                    "Size": 16008609792, "IsBoot": False, "IsSystem": False}]
            return None, *logic.offered_disks(raw)
        rc, out = engine.list_disks()
        if rc is None:
            return T("Linux did not answer the USB disk list within 30 seconds. Unplug card readers and other USB drives, then press Refresh."), [], []
        try:
            if rc != 0:
                raise ValueError((out.strip().splitlines() or ["exit code " + str(rc)])[-1])
            raw = logic.parse_disk_list(out)
        except ValueError as e:
            return T("Could not list USB sticks: ") + str(e), [], []
        own = sysdisks.disks_under(engine.HOME) | sysdisks.disks_under(engine.WORK)
        offered, skipped = logic.offered_disks(raw, own, sysdisks.disk_mounts())
        return None, offered, skipped

    # --- reports and logs ---------------------------------------------------------------------------------------------

    async def offer_operation_report(self, kind):
        report = self.last.report if self.last else None
        if report is None or not report.saved:
            self.say(T("The operation has no saved local report. The visible output remains available to copy.")
                     if report is None else report.failure)
            return
        self.say(T("The local report is saved at ") + report.path + T(". Scan for logs and send them can retry after restart."))
        await self.offer_log(report.path, kind)

    async def offer_log(self, path, kind):
        ask = await self.ok_cancel(F(
            "The {0} failed.\r\n\r\n1401 is sending this log to nullmothsystems.com so the bug can be found and fixed. "
            "It holds this scan's hardware list, bounded device/CPU observations and what the check or build printed. Windows capabilities do not establish macOS support. Your Windows user name and PC name are removed first. "
            "Nothing else on this PC is sent.\r\n\r\nClick OK to send it now, or Cancel to keep it only on this PC.", Op(kind)),
            T("1401 - sending the log"))
        if ask != "ok":
            self.say(T("The log was not sent. It stays in ") + path + T("."))
            return
        batch = upload.new_batch()
        ids = []
        rid = await in_thread(self.sender.send, path, f"1401-{kind}-log.txt", kind + " failed", batch)
        if rid is not None:
            ids.append(rid)
        evidence = reports.find_evidence(engine.WORK, self.scan_run_id, False)
        if evidence is not None:
            rid = await in_thread(self.sender.send, evidence.path, "1401-scan-evidence.json.txt",
                                  kind + " failed; saved evidence from this scan run; macOS support not assessed", batch)
            if rid is not None:
                ids.append(rid)
        self.say(T("Logs sent. Report IDs ") + ", ".join(ids) + T(" - mention them in the NullMoth Discord if you ask for help.")
                 if ids else F("Could not send the logs ({0}). They stay on this PC.", self.sender.last_error))

    async def offer_stick_logs(self, asked):
        """MainForm.OfferStickLogs: saved reports and the startup logs a stick made by 1401 holds, sent after a notice."""
        work = engine.WORK
        found, configs, provenance = [], [], []
        try:
            found += [f for f in reports.find(work) if asked or "-crash-" in os.path.basename(f)]
        except OSError as error:
            self.say(F("Saved reports could not be listed ({0}, code {1}). Check access to the app data folder; existing files were retained.",
                       type(error).__name__, error.errno or 0))
        bl = os.path.join(work, "build-log.txt")
        if asked and os.path.isfile(bl):
            found.append(bl)
        scan_log = os.path.join(work, "scan-log.txt")
        if asked and os.path.isfile(scan_log):
            found.append(scan_log)
        evidence = reports.find_evidence(work, self.scan_run_id, True) if asked else None
        if evidence is not None:
            found.insert(0, evidence.path)
        crash = os.path.join(work, "crash-log.txt")
        if os.path.isfile(crash):
            found.append(crash)
        for root in await in_thread(sysdisks.removable_roots):
            try:
                stick = [os.path.join(root, n) for n in os.listdir(root)
                         if n.lower().startswith("panic-") and n.lower().endswith(".txt")]
                oc = sorted((os.path.join(root, n) for n in os.listdir(root)
                             if n.lower().startswith("opencore-") and n.lower().endswith(".txt")), reverse=True)[:3]
                found += stick + oc
                # the config that booted goes with the logs, serial numbers removed
                cfg = os.path.join(root, "EFI", "OC", "config.plist")
                if (stick or oc) and os.path.isfile(cfg):
                    label = re.sub(r"[^A-Za-z0-9_-]", "_", os.path.basename(root.rstrip("/")) or "stick")
                    safe = os.path.join(work, f"stick-config-{label}.txt")
                    with open(cfg, encoding="utf-8", errors="replace") as fh:
                        redacted = logic.redact_config(fh.read())
                    with open(safe, "w", encoding="utf-8") as fh:
                        fh.write(redacted)
                    configs.append(safe)
                    prov = logic.stick_provenance(root, cfg)
                    if prov:
                        provenance.append(prov)
            except OSError:
                pass
        if not found:
            if asked:
                await self.message(T("No logs were found. Plug in the USB stick 1401 made (the one you started macOS from) and try again."))
            return
        local = [f for f in found if reports.is_local(work, f)]
        other = [f for f in found if not reports.is_local(work, f)]
        ordered = []
        for f in local + configs + other:
            if f not in ordered:
                ordered.append(f)
        found = ordered[:6]   # the site takes 30 uploads per hour from one address
        ask, screen = await self.ask_send(F(
            "1401 found {0} diagnostic file{1} (available scan/build evidence and startup logs from attached sticks).\r\n\r\n"
            "These retain available failure and hardware observations. 1401 is sending them to nullmothsystems.com so the bug can be found and fixed. "
            "They hold what OpenCore and macOS printed while starting, and the stick's OpenCore settings with serial numbers removed. "
            "An included scan receipt records saved Windows hardware observations, even if the check failed; it does not prove this PC or the attached stick has that hardware. Nothing else on this PC is sent.\r\n\r\nClick Send to send them now, or Cancel to keep them locally.",
            len(found), "" if len(found) == 1 else T("s")))
        # what to change comes first, sent or not, and before the logs are moved off the stick
        await self.show_stick_fixes(found, screen)
        if ask != "send":
            return
        what = "startup log from the stick" + ("; screen stopped at: " + screen if screen else "") + \
            ("; " + "; ".join(provenance) if provenance else "")
        foreign = [x for x in provenance if "not built by 1401" in x]
        if foreign:
            await self.message("\n\n".join(foreign) + "\n\nBuild the stick again with 1401: it makes the OpenCore settings "
                               "this PC needs. A stick made elsewhere, or edited afterwards, cannot be fixed from its logs.")
        if screen:
            try:
                with open(os.path.join(work, "stopped-at.txt"), "w", encoding="utf-8") as fh:
                    fh.write(screen)
            except OSError:
                pass
        ids = []
        batch = upload.new_batch()
        for f in found:
            if reports.is_local(work, f):
                local_what = "saved local operation report; original app version is recorded in the payload; not current-PC identity proof"
            else:
                local_what = what
            if evidence is not None and f == evidence.path:
                local_what += ("; this scan run; not macOS qualification" if evidence.current_run else
                               "; saved prior scan; not current-PC or attached-stick identity proof")
            rid = await in_thread(self.sender.send, f, upload.send_name(f), local_what, batch)
            if rid is None:
                continue
            ids.append(rid)
            try:
                if f in (bl, scan_log) or reports.is_local(work, f) or (evidence is not None and f == evidence.path) or f in configs:
                    continue
                if f == crash:
                    os.rename(crash, crash + ".sent-" + GLib.DateTime.new_now_local().format("%Y%m%d-%H%M%S"))
                    continue
                sent = os.path.join(sysdisks.mount_root(f), "NullMoth", "sent-logs")
                os.makedirs(sent, exist_ok=True)
                os.replace(f, os.path.join(sent, os.path.basename(f)))
            except OSError:
                pass
        await self.message(T("Sent ") + str(len(ids)) + T(" log(s). Report ID ") + ", ".join(ids) + T(" - mention it in the NullMoth Discord if you ask for help.")
                           if ids else T("The logs could not be sent: ") + self.sender.last_error + T("\r\nThey are still where they were; try again with Scan for logs and send them."))

    async def ask_send(self, text):
        """MainForm.AskSend: the notice, with an optional line for where the screen stopped."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.append(_label(T("Optional: the last line on the screen when it stopped (for example \"PCI configuration begin\"):")))
        entry = Gtk.Entry(max_length=300)
        box.append(entry)
        r = await self.alert(T("1401 - sending the startup logs"), text, [("cancel", T("Cancel")), ("send", T("Send"))],
                             default="send", close="cancel", extra=box)
        return r, " ".join(entry.get_text().split()).strip()

    async def show_stick_fixes(self, found, screen):
        logs = [f for f in found if os.path.basename(f).lower().startswith("opencore-")]
        if not logs and not screen:
            return
        args = ["p1401.diagnose"]
        for f in logs:
            args += ["--log", f]
        if screen:
            args += ["--stopped", screen.replace("\\", "/").replace('"', "'")]
        report = os.path.join(self.scan_dir, "Report.json") if self.scan_dir else None
        if report and os.path.isfile(report):
            args += ["--report", report]
        res = await self.run(args, show=False)
        shown = logic.diagnose_lines(res.lines)
        if res.rc != 0:
            self.say(F("The stick logs could not be read for fixes (code {0}); they can still be sent.", res.rc))
            return
        if shown:
            await self.message(T("What to change before you start from the stick again:") + "\n\n" + "\n".join(shown).strip())

    # --- updates and the EFI check ------------------------------------------------------------------------------------

    async def check_app_version(self):
        res = await self.run(["p1401.nullmoth", "appcheck", self.version], show=False)
        newer = logic.appcheck_newer(res.rc, res.lines)
        if newer is None:
            return
        self.must_update = newer
        await self.ask_to_update()

    async def ask_to_update(self):
        """MainForm.AskToUpdate. Returns True when the step may go on. On Linux there is no in-place swap of a
        release folder (the Windows update unpacks a Windows zip), so "Not now" lets the person continue."""
        r = await self.alert(T("1401 - Update needed"),
                             f"This is 1401 {self.version}. 1401 {self.must_update} is out, and it fixes problems that stop "
                             "older versions. 1401 should update before it builds or writes anything.\n\nUpdate now?",
                             [("later", T("Not now")), ("ok", T("Update now"))], default="ok", close="later")
        if r == "ok":
            await self.update_app()
            return False
        self.must_update = None
        return True

    async def update_app(self):
        """Update 1401: a git checkout is fast-forwarded (git pull --ff-only); an installed copy is updated by running
        linux/install.sh from the new release, which this opens."""
        if self.busy:
            return
        if not os.path.isdir(os.path.join(engine.HOME, ".git")):
            res = await self.run(["p1401.nullmoth", "appcheck", self.version], show=False)
            r = logic.last_result(res.lines) or ""
            if r.startswith("RESULT uptodate"):
                await self.message(f"1401 {self.version} is the newest version.", "1401 - Update")
                return
            await self.message("Download the newest 1401 from the releases page, then run linux/install.sh from it again. "
                               "Your working files are kept.", "1401 - Update")
            _open_uri(RELEASES)
            return
        self.busy = True
        self.app_link.set_sensitive(False)
        try:
            out = await in_thread(_git_pull, engine.HOME)
        finally:
            self.busy = False
            self.app_link.set_sensitive(True)
        if out[0] != 0:
            await self.message("1401 could not update itself:\n\n" + out[1], "1401 - Update")
            return
        if "Already up to date" in out[1]:
            await self.message(f"1401 {self.version} is the newest version this copy can get.\n\n" + out[1], "1401 - Update")
            return
        if await self.ok_cancel("1401 is updated. 1401 closes and opens again.", "1401 - Update") == "ok":
            os.execv(sys.executable, [sys.executable] + sys.argv)

    async def update_driver(self):
        """Update the NVIDIA driver: into this app's NullMoth folder and onto any plugged-in 1401 stick."""
        if self.busy:
            return
        self.busy = True
        self.drv_link.set_sensitive(False)
        dirs = [engine.nullmoth_dir()]
        for root in await in_thread(sysdisks.removable_roots):
            if os.path.isdir(os.path.join(root, "NullMoth")):
                dirs.append(os.path.join(root, "NullMoth"))
        res = await self.run(["p1401.nullmoth", "update"] + dirs, show=False)
        self.report_save_status()
        self.busy = False
        self.drv_link.set_sensitive(True)
        text = "\n".join(l for l in res.lines if not l.startswith("PROGRESS "))
        await self.message(T("The NVIDIA driver is up to date.\r\n\r\n") + text +
                           ("" if len(dirs) > 1 else T("\r\n\r\nNo 1401 stick was plugged in; plug it in and run this again to update it too."))
                           if res.rc == 0 else T("The driver could not be updated:\r\n\r\n") + text, T("1401 - Update driver"))

    def _pick_folder(self):
        fut = Future()
        dialog = Gtk.FileDialog(title=T("Select the drive or folder that holds your EFI folder"))

        def done(d, result):
            try:
                f = d.select_folder_finish(result)
                fut.resolve(f.get_path() if f else None)
            except GLib.Error:
                fut.resolve(None)
        dialog.select_folder(self, None, done)
        return fut

    async def check_own_efi(self):
        """efidoctor on an EFI someone built themselves: shows what it would change, writes only after a yes."""
        if self.busy:
            return
        root = await self._pick_folder()
        if not root:
            return
        args = ["p1401.efidoctor", root]
        rep = os.path.join(self.scan_dir, "Report.json") if self.scan_dir else None
        if rep and os.path.isfile(rep):
            args += ["--report", rep]
        try:
            mount = sysdisks.mount_root(root)
            logs = sorted(n for n in os.listdir(mount) if n.startswith("opencore-") and n.endswith(".txt"))
            if logs:
                args += ["--boot-log", os.path.join(mount, logs[-1])]
        except OSError:
            pass
        self.busy = True
        res = await self.run(args, show=False)
        self.busy = False
        text = "\n".join(logic.doctor_lines(res.lines))
        if res.rc == 0:
            await self.message(T("Your EFI passed every check 1401 makes.\r\n\r\n") + text)
            return
        if not logic.doctor_fixable(res.lines):
            await self.message(T("1401 found problems it cannot fix by itself:\r\n\r\n") + text)
            return
        if await self.alert("1401", text + T("\r\n\r\nFix these in your config.plist now? The current one is kept as a backup next to it."),
                            [("no", T("No")), ("yes", T("Yes"))], default="no", close="no") != "yes":
            return
        self.busy = True
        res = await self.run(args + ["--fix"], show=False)
        self.busy = False
        await self.message("\n".join(logic.doctor_lines(res.lines)))

    # --- verification without a PC to scan ----------------------------------------------------------------------------

    def verify_navigation(self):
        """The page gating, exercised through the real handlers with synthetic state (MainForm's packaged check)."""
        import tempfile
        directory = tempfile.mkdtemp(prefix="1401-firmware-ui-")
        guide = os.path.join(directory, "firmware.html")
        with open(guide, "w") as fh:
            fh.write("<!doctype html><title>Firmware prerequisite fixture</title><h1>Firmware prerequisites</h1>")
        checks = []

        def check(name, cond):
            checks.append((name, bool(cond)))
        try:
            self.busy = False; self.scanned = True; self.built = self.written = self.showing_prerequisites = False
            self.firmware_guide_file = None
            self.go(1); check("no guide, no link", not self.firmware_link.get_visible())
            self.firmware_guide_file = guide
            self.go(1); check("guide link shown on the check page", self.firmware_link.get_visible() and self.firmware_link.get_sensitive())
            self.firmware_link.emit("clicked")
            check("link opens the prerequisites", self.page == 3 and self.showing_prerequisites and self.prerequisite_return_page == 1
                  and self.title_label.get_label() == T("Firmware prerequisites") and self.next.get_label() == T("Return >")
                  and self.guide.get_visible() and self.next.get_sensitive() and not self.built)
            self.next.emit("clicked")
            check("Return goes back to the check", self.page == 1 and not self.showing_prerequisites)
            self.firmware_link.emit("clicked"); self.back.emit("clicked")
            check("Back goes back to the check", self.page == 1 and not self.showing_prerequisites)
            self.go(2); self.firmware_link.emit("clicked")
            check("link from a refused build returns to it", self.page == 3 and self.prerequisite_return_page == 2)
            self.next.emit("clicked"); check("Return to the build", self.page == 2 and not self.built)
            self.go(3); check("BIOS steps need a build", not self.next.get_sensitive() and self.title_label.get_label() == T("Your BIOS steps"))
            self.built = True; self.go(3); check("BIOS steps after a build", self.next.get_sensitive())
            self.built = False; self.go(2); self.page = 4; self.next.emit("clicked")
            check("the stick page sends an unbuilt PC back to Build", self.page == 2 and not self.disk_row.get_visible())
            self.busy = True; self.firmware_link.emit("clicked")
            check("no navigation while busy", self.page == 2 and not self.showing_prerequisites)
            self.busy = False; self.firmware_guide_file = os.path.join(directory, "missing.html"); self.go(2)
            check("missing guide file hides the link", not self.firmware_link.get_visible())
            self.scanned = False; self.go(2); check("Build needs a check", not self.next.get_sensitive())
            self.scanned = self.built = True; self.summary = "macOS Sequoia for this PC\nNullMoth NVIDIA driver"
            self.go(4); check("NVIDIA stick offers ARM apps", self.arm_apps.get_visible())
            self.summary = "macOS Sequoia for this PC"; self.go(4); check("other sticks do not", not self.arm_apps.get_visible())
        finally:
            self.busy = False; self.scanned = self.built = self.written = self.showing_prerequisites = False
            self.firmware_guide_file = None; self.summary = ""
            self.go(0)
            for n in os.listdir(directory):
                os.remove(os.path.join(directory, n))
            os.rmdir(directory)
        return checks


def _git_pull(home):
    import subprocess
    try:
        p = subprocess.run(["git", "-C", home, "pull", "--ff-only"], capture_output=True, text=True, timeout=300)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)


class App(Adw.Application):
    def __init__(self, dry_run=False, screenshot=None, self_check=False):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.dry_run, self.screenshot, self.self_check = dry_run, screenshot, self_check
        self.exit_code = 0
        self.window = None

    def do_activate(self):
        from gi.repository import Gdk
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.IconTheme.get_for_display(display).add_search_path(engine.LINUX_DIR)
        self.window = Window(self, dry_run=self.dry_run or self.screenshot is not None or self.self_check)
        self.window.present()
        if self.self_check:
            GLib.timeout_add(300, self._run_self_check)
        elif self.screenshot is not None:
            GLib.timeout_add(500, self._shoot, 0)
        elif not self.dry_run:
            # as on Windows: stick logs first, then the version check
            GLib.idle_add(lambda: (self.window.spawn(self.window.offer_stick_logs(False)), False)[1])
            GLib.idle_add(lambda: (self.window.spawn(self.window.check_app_version()), False)[1])

    def _run_self_check(self):
        checks = self.window.verify_navigation()
        for name, ok in checks:
            print(("ok   " if ok else "FAIL ") + name)
        self.exit_code = 0 if all(ok for _, ok in checks) else 1
        self.quit()
        return False

    def _shoot(self, step):
        w = self.window
        states = [(0, {}), (1, {"scanned": False}), (2, {"scanned": True}), (4, {"scanned": True, "built": True,
                  "summary": "macOS Sequoia for this PC, as a MacPro7,1.\nNullMoth NVIDIA driver"}), (5, {})]
        if step >= len(states):
            self.quit()
            return False
        page, state = states[step]
        for k, v in state.items():
            setattr(w, k, v)
        w.go(page)
        GLib.timeout_add(700, self._save_shot, step, page)
        return False

    def _save_shot(self, step, page, tries=0):
        w = self.window
        os.makedirs(self.screenshot, exist_ok=True)
        try:
            paintable = Gtk.WidgetPaintable.new(w)
            width, height = w.get_width(), w.get_height()
            snap = Gtk.Snapshot()
            paintable.snapshot(snap, width, height)
            node = snap.to_node()
            if node is None and tries < 10:   # nothing drawn yet: try again on the next frames
                GLib.timeout_add(200, self._save_shot, step, page, tries + 1)
                return False
            if node is None:
                print(f"page {page}: nothing was drawn", file=sys.stderr)
                self.exit_code = 1
            else:
                texture = w.get_renderer().render_texture(node, None)
                texture.save_to_png(os.path.join(self.screenshot, f"1401-page{page}.png"))
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            self.exit_code = 1
        GLib.idle_add(self._shoot, step + 1)
        return False


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="1401", description="1401 for Linux (GTK 4 + libadwaita)")
    ap.add_argument("--dry-run", action="store_true", help="open the window without running any engine step")
    ap.add_argument("--self-check", action="store_true", help="check the page gating through the real handlers and exit")
    ap.add_argument("--screenshot", metavar="DIR", help="draw each page to DIR/1401-page<N>.png and exit")
    a = ap.parse_args(argv)
    if os.geteuid() == 0 and not os.environ.get("A1401_ALLOW_ROOT_WINDOW"):
        print("1401: do not run the window as root. Start it as yourself; it asks for the administrator password "
              "(pkexec) only for the two steps that need it.", file=sys.stderr)
        return 1
    app = App(dry_run=a.dry_run, screenshot=a.screenshot, self_check=a.self_check)
    rc = app.run([sys.argv[0]])
    return app.exit_code or rc

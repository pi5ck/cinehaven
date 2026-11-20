"""'My Videos' page: everything on this computer's drives."""
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from . import localmedia as L  # noqa: E402

FILTERS = [("all", "All"), ("movie", "Movies"), ("tv", "TV episodes"), ("home", "Home & other")]
SORTS = [("recent", "Recently added"), ("title", "Title A–Z"), ("year", "Year")]

CSS = b"""
.video-thumb { border-radius: 10px; background: alpha(currentColor,.08); }
.resume-bar trough, .resume-bar progress { min-height: 4px; }
"""


class VideoCard(Gtk.Box):
    def __init__(self, page, v):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4, css_classes=["movie-card"],
                         valign=Gtk.Align.START)
        self.v = v
        ov = Gtk.Overlay()
        self.pic = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, css_classes=["video-thumb"], can_shrink=True)
        self.pic.set_size_request(220, 124)
        self.pic.set_overflow(Gtk.Overflow.HIDDEN)
        ov.set_child(self.pic)
        self.icon = Gtk.Image(icon_name="video-x-generic-symbolic", pixel_size=40, opacity=0.4)
        ov.add_overlay(self.icon)
        pos = page.win.settings.get("resume", {}).get(v["path"])
        if pos and pos.get("dur"):
            bar = Gtk.ProgressBar(fraction=min(1, pos["t"] / pos["dur"]), valign=Gtk.Align.END,
                                  css_classes=["resume-bar", "osd"], margin_start=6, margin_end=6, margin_bottom=6)
            ov.add_overlay(bar)
        self.append(ov)
        title = v["title"]
        if v["kind"] == "tv" and v.get("season") is not None:
            title += f" · S{v['season']:02d}E{v['episode']:02d}"
        self.append(Gtk.Label(label=title, xalign=0, wrap=True, lines=2, max_width_chars=24,
                              ellipsize=Pango.EllipsizeMode.END, css_classes=["heading"]))
        meta = " · ".join(x for x in (str(v["year"]) if v.get("year") else None,
                                       L.human_size(v["size"]), L.drive_label(v["path"])) if x)
        self.append(Gtk.Label(label=meta, xalign=0, ellipsize=Pango.EllipsizeMode.END,
                              css_classes=["dim-label", "caption"]))
        if v.get("incomplete"):
            self.append(Gtk.Label(label="⚠ Incomplete download", xalign=0, css_classes=["badge", "warning"],
                                  halign=Gtk.Align.START))
        self.set_tooltip_text(v["path"])
        rc = Gtk.GestureClick(button=3)
        rc.connect("pressed", lambda *_: page.menu_for(self))
        self.add_controller(rc)
        page.request_thumb(self)

    def set_thumb(self, tex):
        self.pic.set_paintable(tex)
        self.icon.set_visible(False)


class MyVideosPage(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.videos = L.load()
        self.kind = "all"
        self.thumb_pool = ThreadPoolExecutor(max_workers=2)
        self.scanning = False
        self.last_scan = None

        top = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        r1 = Gtk.Box(spacing=8)
        self.entry = Gtk.SearchEntry(placeholder_text="Search your videos…", hexpand=True, css_classes=["big-search"])
        self.entry.connect("search-changed", lambda *_: self.flow.invalidate_filter())
        r1.append(self.entry)
        self.sort = Gtk.DropDown.new_from_strings([x[1] for x in SORTS])
        self.sort.set_valign(Gtk.Align.CENTER)
        self.sort.connect("notify::selected", lambda *_: self.flow.invalidate_sort())
        r1.append(self.sort)
        self.rescan_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Scan drives again",
                                     valign=Gtk.Align.CENTER, css_classes=["flat", "circular"])
        self.rescan_btn.connect("clicked", lambda *_: self.rescan())
        r1.append(self.rescan_btn)
        add = Gtk.Button(icon_name="folder-new-symbolic", tooltip_text="Add a folder to scan",
                         valign=Gtk.Align.CENTER, css_classes=["flat", "circular"])
        add.connect("clicked", lambda *_: self.add_folder())
        r1.append(add)
        top.append(r1)
        chips = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
        group = None
        self.chip_btns = {}
        for key, label in FILTERS:
            b = Gtk.ToggleButton(label=label, group=group, active=(key == "all"), css_classes=["pill"])
            group = group or b
            b.connect("toggled", lambda btn, k=key: btn.get_active() and self._set_kind(k))
            chips.append(b)
            self.chip_btns[key] = b
        top.append(chips)
        self.status = Gtk.Label(css_classes=["dim-label"])
        top.append(self.status)
        clamp = Adw.Clamp(maximum_size=1000, margin_top=16, margin_start=16, margin_end=16, margin_bottom=6)
        clamp.set_child(top)
        self.append(clamp)

        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True, valign=Gtk.Align.START,
                                max_children_per_line=8, min_children_per_line=1, column_spacing=8,
                                row_spacing=12, margin_start=16, margin_end=16, margin_bottom=24,
                                activate_on_single_click=True)
        self.flow.set_filter_func(self._filter)
        self.flow.set_sort_func(self._sort)
        self.flow.connect("child-activated", lambda fb, ch: self.play(ch.get_child().v))
        sc = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        sc.set_child(self.flow)
        self.stack = Gtk.Stack()
        self.stack.add_named(sc, "grid")
        self.stack.add_named(Adw.StatusPage(icon_name="video-x-generic-symbolic", title="No videos found yet",
                                            description="CineHaven looks in your home folder and on every "
                                                        "connected drive. Plug in a drive or add a folder."), "empty")
        self.append(self.stack)
        self._fill()

        # Re-scan automatically when a USB drive / disk is mounted or removed
        self.vm = Gio.VolumeMonitor.get()
        self.vm.connect("mount-added", lambda *_: GLib.timeout_add_seconds(2, lambda: (self.rescan(), False)[1]))
        self.vm.connect("mount-removed", lambda *_: GLib.timeout_add_seconds(1, lambda: (self.rescan(), False)[1]))
        GLib.timeout_add_seconds(3, lambda: (self.rescan(), False)[1])     # fresh scan shortly after start
        GLib.timeout_add_seconds(1800, lambda: (self.rescan(), True)[1])   # and every 30 minutes

    # ── data ─────────────────────────────────────────────────────────────
    def _fill(self):
        self.flow.remove_all()
        for v in self.videos:
            self.flow.append(VideoCard(self, v))
        self._update_status()

    def _update_status(self):
        n = len(self.videos)
        kinds = {k: sum(1 for v in self.videos if v["kind"] == k) for k in ("movie", "tv", "home")}
        for key, label in FILTERS:
            cnt = n if key == "all" else kinds.get(key, 0)
            self.chip_btns[key].set_label(f"{label} {cnt}" if cnt else label)
        drives = len({L.drive_label(v["path"]).split(" › ")[0] for v in self.videos})
        if self.scanning:
            txt = f"Scanning your drives… {self._scan_found} videos found in {self._scan_dirs:,} folders"
        else:
            txt = f"{n} videos on {drives} drive{'s' if drives != 1 else ''}"
            if self.last_scan:
                txt += " · up to date"
        self.status.set_label(txt)
        self.stack.set_visible_child_name("grid" if n or self.scanning else "empty")

    def rescan(self):
        if self.scanning:
            return
        self.scanning = True
        self._scan_found = self._scan_dirs = 0
        self.rescan_btn.set_sensitive(False)
        self._update_status()

        def prog(dirs, found):
            self._scan_dirs, self._scan_found = dirs, found
            GLib.idle_add(self._update_status)

        def work():
            try:
                vids = L.scan(prog, stop=lambda: self.win.engine.closing)
            except Exception as e:
                print("scan failed:", e)
                vids = None
            GLib.idle_add(done, vids)

        def done(vids):
            self.scanning = False
            self.rescan_btn.set_sensitive(True)
            self.last_scan = time.time()
            if vids is not None:
                old = {v["path"]: v for v in self.videos}
                changed = [v["path"] for v in vids] != list(old) or any(
                    old.get(v["path"], {}).get("mtime") != v["mtime"] for v in vids)
                self.videos = vids + [v for v in self._extra_videos() if v["path"] not in {x["path"] for x in vids}]
                if changed:
                    self._fill()
            self._update_status()
        threading.Thread(target=work, daemon=True).start()

    def _extra_videos(self):
        out = []
        for folder in self.win.settings.get("extra_folders", []):
            for dirpath, dirs, files in os.walk(folder):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for f in files:
                    p = os.path.join(dirpath, f)
                    if os.path.splitext(f)[1].lower() in L.EXTS:
                        try:
                            st = os.stat(p)
                        except OSError:
                            continue
                        if st.st_size >= 5 * 1024 * 1024:
                            v = L.parse(p, st.st_size)
                            v.update(path=p, size=st.st_size, mtime=st.st_mtime,
                                     incomplete=st.st_blocks * 512 < st.st_size * 0.9)
                            out.append(v)
        return out

    def add_folder(self):
        fd = Gtk.FileDialog(title="Choose a folder with videos")

        def done(d, res):
            try:
                f = d.select_folder_finish(res)
            except GLib.Error:
                return
            if f and f.get_path():
                ex = self.win.settings.setdefault("extra_folders", [])
                if f.get_path() not in ex:
                    ex.append(f.get_path())
                    self.win.save_settings()
                self.rescan()
        fd.select_folder(self.win, None, done)

    # ── thumbnails ───────────────────────────────────────────────────────
    def request_thumb(self, card):
        path = card.v["path"]
        if card.v.get("incomplete"):
            return

        def work():
            t = L.thumbnail(path)
            if t:
                GLib.idle_add(apply, t)

        def apply(t):
            try:
                card.set_thumb(Gdk.Texture.new_from_filename(t))
            except GLib.Error:
                pass
            return False
        self.thumb_pool.submit(work)

    # ── filter / sort ────────────────────────────────────────────────────
    def _set_kind(self, k):
        self.kind = k
        self.flow.invalidate_filter()

    def _filter(self, child):
        v = child.get_child().v
        if self.kind != "all" and v["kind"] != self.kind:
            return False
        q = self.entry.get_text().strip().lower()
        return not q or q in v["title"].lower() or q in v["path"].lower()

    def _sort(self, a, b):
        va, vb = a.get_child().v, b.get_child().v
        mode = SORTS[self.sort.get_selected()][0]
        if mode == "title":
            ka, kb = (va["title"].lower(), va.get("season") or 0, va.get("episode") or 0), \
                     (vb["title"].lower(), vb.get("season") or 0, vb.get("episode") or 0)
        elif mode == "year":
            ka, kb = -(va.get("year") or 0), -(vb.get("year") or 0)
        else:
            ka, kb = -va["mtime"], -vb["mtime"]
        return (ka > kb) - (ka < kb)

    # ── actions ──────────────────────────────────────────────────────────
    def play(self, v):
        if not os.path.exists(v["path"]):
            self.win.toast("That file has been moved or its drive is disconnected")
            self.rescan()
            return
        title = v["title"] + (f" ({v['year']})" if v.get("year") else "")
        self.win.play_local(v["path"], title=title)

    def menu_for(self, card):
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6, margin_bottom=6,
                      margin_start=6, margin_end=6)
        for label, cb in (("▶  Play", lambda: self.play(card.v)),
                          ("⟲  Play from the start", lambda: (self.win.forget_resume(card.v["path"]),
                                                             self.play(card.v))),
                          ("📁  Show in folder", lambda: Gtk.FileLauncher.new(
                              Gio.File.new_for_path(card.v["path"])).open_containing_folder(self.win, None, None))):
            b = Gtk.Button(label=label, css_classes=["flat"])
            b.get_child().set_xalign(0)
            b.connect("clicked", lambda _b, c=cb: (pop.popdown(), c()))
            box.append(b)
        pop.set_child(box)
        pop.set_parent(card)
        pop.connect("closed", lambda p: GLib.idle_add(p.unparent))
        pop.popup()

    def shutdown(self):
        self.thumb_pool.shutdown(wait=False, cancel_futures=True)

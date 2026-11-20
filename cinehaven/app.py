"""CineHaven – search, stream-while-downloading, and play movies."""
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from . import APP_ID, APP_NAME, APP_VERSION, DATA_DIR, DOWNLOAD_DIR  # noqa: E402
from . import catalog, health, safety, translate  # noqa: E402
from .doctor import DownloadDoctor, PlaybackDoctor  # noqa: E402
from .engine import Engine, fetch, is_video, normalize_magnet  # noqa: E402
from .player import VideoView  # noqa: E402
from .mylist import CSS as MYLIST_CSS, MyListPage, StreamCard, streaming_docs  # noqa: E402
from .livetv_ui import CSS as LIVE_CSS, LiveTVPage  # noqa: E402
from .localmedia_ui import CSS as LOCAL_CSS, MyVideosPage  # noqa: E402

SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
BADGE = {catalog.VERIFIED: ("✔ Verified", "success"),
         catalog.LIKELY_OK: ("✔ Free to share", "accent"),
         catalog.UNVERIFIED: ("⚠ Unverified", "warning")}

CSS = b"""
.poster { border-radius: 10px; background: alpha(currentColor, .08); }
.movie-card { padding: 6px; border-radius: 14px; }
.movie-card:hover { background: alpha(currentColor, .06); }
.badge { font-size: 0.78em; font-weight: 700; padding: 2px 8px; border-radius: 99px; }
.badge.success { background: alpha(@success_color, .18); color: @success_color; }
.badge.accent  { background: alpha(@accent_color, .18);  color: @accent_color; }
.badge.warning { background: alpha(@warning_color, .2);  color: @warning_color; }
.player-bg { background: black; }
.controls { padding: 10px 14px; margin: 12px; border-radius: 16px; }
.big-search { font-size: 1.15em; min-height: 44px; }
.buffer-box { padding: 26px 34px; border-radius: 20px; }
.hero-title { font-size: 1.6em; font-weight: 800; }
"""


def human_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024


def fmt_time(t):
    if t is None:
        return "--:--"
    t = int(t)
    h, m, s = t // 3600, t % 3600 // 60, t % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def load_settings():
    return health.load_json(SETTINGS_FILE, {})


def save_settings(s):
    try:
        health.save_json(SETTINGS_FILE, s)
    except OSError as e:
        health.log.error("Couldn't save settings: %s", e)


# ──────────────────────────────────────────────────────────────────────────
class MovieCard(Gtk.Box):
    def __init__(self, win, r):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.r = r
        self.add_css_class("movie-card")
        self.set_size_request(170, -1)
        self.pic = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
        self.pic.set_size_request(160, 220)
        self.pic.add_css_class("poster")
        self.pic.set_overflow(Gtk.Overflow.HIDDEN)
        self.append(self.pic)
        t = Gtk.Label(label=r["title"], wrap=True, lines=2, xalign=0, max_width_chars=18,
                      ellipsize=Pango.EllipsizeMode.END, css_classes=["heading"])
        t.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.append(t)
        row = Gtk.Box(spacing=6)
        row.append(Gtk.Label(label=str(r["year"] or "—"), css_classes=["dim-label", "caption"]))
        text, cls = BADGE[r["verdict"]]
        row.append(Gtk.Label(label=text, css_classes=["badge", cls], tooltip_text=r["reason"]))
        self.append(row)
        win.load_thumb(r["thumb"], self.pic)


class LibraryRow(Gtk.ListBoxRow):
    def __init__(self, win, item):
        super().__init__(activatable=False)
        self.win, self.item = win, item
        box = Gtk.Box(spacing=12, margin_top=10, margin_bottom=10, margin_start=12, margin_end=12)
        self.set_child(box)
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        self.title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["heading"])
        self.sub = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["dim-label", "caption"])
        self.bar = Gtk.ProgressBar()
        info.append(self.title)
        info.append(self.sub)
        info.append(self.bar)
        box.append(info)
        self.play_btn = Gtk.Button(icon_name="media-playback-start-symbolic", tooltip_text="Watch",
                                   valign=Gtk.Align.CENTER, css_classes=["suggested-action", "circular"])
        self.play_btn.connect("clicked", lambda *_: win.watch_item(item))
        self.pause_btn = Gtk.Button(valign=Gtk.Align.CENTER, css_classes=["flat", "circular"])
        self.pause_btn.connect("clicked", self._toggle)
        folder = Gtk.Button(icon_name="folder-open-symbolic", tooltip_text="Show in folder",
                            valign=Gtk.Align.CENTER, css_classes=["flat", "circular"])
        folder.connect("clicked", lambda *_: win.open_folder(item))
        rm = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Remove",
                        valign=Gtk.Align.CENTER, css_classes=["flat", "circular"])
        rm.connect("clicked", lambda *_: win.confirm_remove(item))
        for b in (self.play_btn, self.pause_btn, folder, rm):
            box.append(b)
        self.refresh()

    def _toggle(self, *_):
        self.item.resume() if self.item.is_paused() else self.item.pause()
        self.refresh()

    def refresh(self):
        it = self.item
        st = it.handle.status()
        self.title.set_label(it.title)
        frac = it.file_progress()
        self.bar.set_fraction(frac)
        paused = it.is_paused()
        self.pause_btn.set_icon_name("media-playback-start-symbolic" if paused else "media-playback-pause-symbolic")
        self.pause_btn.set_tooltip_text("Resume download" if paused else "Pause download")
        trust = {"verified": "✔ Verified source", "likely": "✔ Free-to-share source",
                 "magnet": "Your link", "unverified": "⚠ Unverified source"}.get(it.meta.get("trust"), "")
        if it.verified is True:
            trust = "✔ Checksum matches original"
        elif it.verified is False:
            trust = "⚠ Checksum differs from Archive listing"
        if not it.has_metadata():
            state = "Finding peers and fetching details…"
        elif frac >= 1:
            state = f"Ready · {human_bytes(it.file_info()[2])}"
            if st.upload_rate > 1024:
                state += f" · sharing {human_bytes(st.upload_rate)}/s"
        elif paused:
            state = f"Paused · {frac*100:.0f}%"
        else:
            state = (f"{frac*100:.0f}% · ↓ {human_bytes(st.download_rate)}/s · "
                     f"{st.num_peers} peers")
            if st.download_rate > 0:
                left = it.file_info()[2] * (1 - frac)
                state += f" · {fmt_time(left / st.download_rate)} left"
        if st.errc and st.errc.value():
            state = "Error: " + st.errc.message()
        self.sub.set_label(" · ".join(x for x in (state, trust) if x))
        self.play_btn.set_sensitive(it.file_index is not None)


# ──────────────────────────────────────────────────────────────────────────
class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME, default_width=1180, default_height=760)
        self.app = app
        self.engine = app.engine
        self.settings = app.settings
        self.thumbs = ThreadPoolExecutor(max_workers=6)
        self.thumb_cache = {}
        self.search_gen = 0
        self.rows = {}
        self.current = None           # Item being watched (or None for local file)
        self.pending_play = None
        self.hide_src = None
        self.verifying = set()
        self.tr_track = None          # subtitle track being live-translated to English
        self.tr_pool = ThreadPoolExecutor(max_workers=2)
        self._sub_text = ""
        self._lang_done = False
        self.live = False
        self._live_t = 0
        self.local_path = None        # file on disk being played (for resume)

        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)
        self.root = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.toasts.set_child(self.root)
        self.root.add_named(self._build_browse(), "browse")
        self.root.add_named(self._build_player(), "player")

        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        self.add_controller(drop)

        self.doctor = PlaybackDoctor(self)
        self.dl_doctor = DownloadDoctor(self)
        GLib.timeout_add(1000, self._tick)
        GLib.timeout_add(2500, self._report_selfcheck)
        GLib.timeout_add_seconds(30, lambda: (self.check_updates(), False)[1])
        GLib.timeout_add(250, self._player_tick)
        self.connect("close-request", self._on_close)
        if self.category == "docs":
            self._set_category("docs")
        else:
            self.run_search("")

    # ── browse UI ─────────────────────────────────────────────────────────
    def _build_browse(self):
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        self.views = Adw.ViewStack()
        sw = Adw.ViewSwitcher(stack=self.views, policy=Adw.ViewSwitcherPolicy.WIDE)
        hb.set_title_widget(sw)

        open_menu = Gio.Menu()
        open_menu.append("Paste magnet link…", "win.open-magnet")
        open_menu.append("Open .torrent file…", "win.open-torrent")
        open_menu.append("Play a video file…", "win.open-video")
        ob = Gtk.MenuButton(icon_name="list-add-symbolic", menu_model=open_menu,
                            tooltip_text="Open link, torrent or video")
        hb.pack_start(ob)

        menu = Gio.Menu()
        menu.append("English movies only", "win.english-only")
        menu.append("Auto English subtitles", "win.auto-english")
        menu.append("Show unverified titles", "win.show-unverified")
        menu.append("Open downloads folder", "win.open-downloads")
        menu.append("Check for updates", "win.check-updates")
        menu.append("Help & diagnostics", "win.diagnostics")
        menu.append(f"About {APP_NAME}", "win.about")
        hb.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, primary=True))
        tv.add_top_bar(hb)

        for name, cb in (("open-magnet", self.ask_magnet), ("open-torrent", self.pick_torrent),
                         ("open-video", self.pick_video), ("about", self.show_about),
                         ("diagnostics", self.show_diagnostics),
                         ("check-updates", lambda *_: self.check_updates(manual=True)),
                         ("open-downloads", lambda *_: Gio.AppInfo.launch_default_for_uri(
                             GLib.filename_to_uri(DOWNLOAD_DIR), None))):
            a = Gio.SimpleAction(name=name)
            a.connect("activate", cb)
            self.add_action(a)
        su = Gio.SimpleAction.new_stateful("show-unverified", None,
                                           GLib.Variant.new_boolean(self.settings.get("show_unverified", False)))
        su.connect("change-state", self._toggle_unverified)
        self.add_action(su)
        for name, key in (("english-only", "english_only"), ("auto-english", "auto_english")):
            act = Gio.SimpleAction.new_stateful(name, None, GLib.Variant.new_boolean(self.settings.get(key, True)))
            act.connect("change-state", self._toggle_setting, key)
            self.add_action(act)

        # Discover
        disc = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        clamp = Adw.Clamp(maximum_size=640, margin_top=18, margin_bottom=8, margin_start=16, margin_end=16)
        self.search = Gtk.SearchEntry(placeholder_text="Search free movies — classics, cartoons, noir, open films…",
                                      css_classes=["big-search"], search_delay=450)
        self.search.connect("search-changed", lambda e: self.run_search(e.get_text()))
        self.search.connect("activate", lambda e: self.run_search(e.get_text()))
        clamp.set_child(self.search)
        disc.append(clamp)
        self.category = self.settings.get("discover_category", "movies")
        cats = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER, margin_bottom=6)
        grp = None
        self.cat_btns = {}
        for key, label in (("movies", "🎬 Movies"), ("docs", "🌍 Documentaries")):
            b = Gtk.ToggleButton(label=label, group=grp, active=(key == self.category), css_classes=["pill"])
            grp = grp or b
            self.cat_btns[key] = b
            b.connect("toggled", lambda btn, k=key: btn.get_active() and self._set_category(k))
            cats.append(b)
        disc.append(cats)
        self.status_lbl = Gtk.Label(css_classes=["dim-label"], margin_bottom=6)
        disc.append(self.status_lbl)
        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                                valign=Gtk.Align.START, column_spacing=6, row_spacing=10,
                                max_children_per_line=12, min_children_per_line=1,
                                margin_start=16, margin_end=16, margin_bottom=24, activate_on_single_click=True)
        self.flow.connect("child-activated", lambda fb, ch: self.show_details(ch.get_child().r))
        self.disc_stack = Gtk.Stack()
        sc = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        inner.append(self._build_doc_shelf())
        self.free_lbl = Gtk.Label(xalign=0, css_classes=["title-4"], margin_start=22, margin_top=6,
                                  margin_bottom=4, visible=False)
        inner.append(self.free_lbl)
        inner.append(self.flow)
        sc.set_child(inner)
        self.disc_stack.add_named(sc, "results")
        self.disc_stack.add_named(Adw.StatusPage(icon_name="system-search-symbolic", title="Nothing found",
                                                 description="Try another title, actor or genre."), "empty")
        sp = Adw.Spinner() if hasattr(Adw, "Spinner") else Gtk.Spinner(spinning=True)
        sp.set_size_request(48, 48)
        sp.set_halign(Gtk.Align.CENTER)
        sp.set_valign(Gtk.Align.CENTER)
        self.disc_stack.add_named(sp, "loading")
        self.disc_stack.add_named(Adw.StatusPage(icon_name="network-offline-symbolic", title="Can't reach the catalogue",
                                                 description="Check your internet connection and try again."), "error")
        disc.append(self.disc_stack)
        self.views.add_titled_with_icon(disc, "discover", "Discover", "system-search-symbolic")

        # My Videos – every video on this computer's drives
        self.myvideos = MyVideosPage(self)
        self.views.add_titled_with_icon(self.myvideos, "videos", "My Videos", "folder-videos-symbolic")

        # Live TV – free channels, health-checked
        self.livetv = LiveTVPage(self)
        self.views.add_titled_with_icon(self.livetv, "live", "Live TV", "video-display-symbolic")
        self.views.connect("notify::visible-child-name",
                           lambda v, _p: v.get_visible_child_name() == "live" and self.livetv.on_shown())

        # My List – where to watch legally (South Africa / UAE)
        self.mylist = MyListPage(self)
        self.views.add_titled_with_icon(self.mylist, "mylist", "My List", "starred-symbolic")

        # Library
        lib = Gtk.Stack()
        self.lib_stack = lib
        lib.add_named(Adw.StatusPage(icon_name="folder-download-symbolic", title="No downloads yet",
                                     description="Pick a movie in Discover, or paste a magnet link with +."), "empty")
        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"],
                                   valign=Gtk.Align.START)
        lc = Adw.Clamp(maximum_size=900, margin_top=18, margin_bottom=18, margin_start=12, margin_end=12)
        lc.set_child(self.listbox)
        ls = Gtk.ScrolledWindow(vexpand=True)
        ls.set_child(lc)
        lib.add_named(ls, "list")
        self.lib_page = self.views.add_titled_with_icon(lib, "library", "Library", "folder-download-symbolic")
        for it in list(self.engine.items.values()):
            self._add_row(it)
        self._update_lib_empty()

        tv.set_content(self.views)
        bar = Adw.ViewSwitcherBar(stack=self.views)
        tv.add_bottom_bar(bar)
        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 560sp"))
        bp.add_setter(bar, "reveal", True)
        bp.add_setter(hb, "show-title", False)
        self.add_breakpoint(bp)
        return tv

    # ── player UI ─────────────────────────────────────────────────────────
    def _build_player(self):
        ov = Gtk.Overlay(css_classes=["player-bg"])
        self.overlay = ov
        self.video = VideoView()
        ov.set_child(self.video)

        # top bar
        top = Gtk.Box(spacing=8, css_classes=["osd", "controls"], valign=Gtk.Align.START)
        back = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text="Back (Esc)", css_classes=["flat", "circular"])
        back.connect("clicked", lambda *_: self.leave_player())
        top.append(back)
        self.p_title = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END, css_classes=["heading"])
        top.append(self.p_title)
        self.p_trust = Gtk.Label(css_classes=["badge", "success"])
        top.append(self.p_trust)
        self.top_rev = Gtk.Revealer(child=top, valign=Gtk.Align.START, reveal_child=True,
                                    transition_type=Gtk.RevealerTransitionType.CROSSFADE)
        ov.add_overlay(self.top_rev)

        # bottom controls
        ctl = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, css_classes=["osd", "controls"])
        self.seek = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True, draw_value=False,
                              show_fill_level=True, restrict_to_fill_level=False)
        self.seek.set_range(0, 1)
        self.seek.connect("change-value", self._on_seek)
        ctl.append(self.seek)
        row = Gtk.Box(spacing=6)
        self.pp = Gtk.Button(icon_name="media-playback-pause-symbolic", tooltip_text="Play/Pause (Space)",
                             css_classes=["flat", "circular"])
        self.pp.connect("clicked", lambda *_: self.toggle_pause())
        row.append(self.pp)
        for icon, secs, tip in (("media-seek-backward-symbolic", -10, "Back 10s (←)"),
                                ("media-seek-forward-symbolic", 30, "Forward 30s (→)")):
            b = Gtk.Button(icon_name=icon, tooltip_text=tip, css_classes=["flat", "circular"])
            b.connect("clicked", lambda _b, s=secs: self.seek_rel(s))
            row.append(b)
        self.time_lbl = Gtk.Label(label="0:00 / --:--", css_classes=["numeric", "caption"], margin_start=6)
        row.append(self.time_lbl)
        self.dl_lbl = Gtk.Label(hexpand=True, xalign=1, css_classes=["dim-label", "caption", "numeric"], margin_end=6)
        row.append(self.dl_lbl)
        self.vol = Gtk.ScaleButton.new(0, 150, 5, ["audio-volume-muted-symbolic", "audio-volume-high-symbolic",
                                                    "audio-volume-low-symbolic", "audio-volume-medium-symbolic"])
        self.vol.set_value(self.settings.get("volume", 100))
        self.vol.connect("value-changed", self._on_volume)
        row.append(self.vol)
        tracks = Gtk.MenuButton(label="CC", tooltip_text="Subtitles & audio tracks",
                                css_classes=["flat"], direction=Gtk.ArrowType.UP)
        tracks.set_create_popup_func(self._build_tracks_popover)
        row.append(tracks)
        fs = Gtk.Button(icon_name="view-fullscreen-symbolic", tooltip_text="Fullscreen (F)",
                        css_classes=["flat", "circular"])
        fs.connect("clicked", lambda *_: self.toggle_fullscreen())
        row.append(fs)
        ctl.append(row)
        self.bot_rev = Gtk.Revealer(child=ctl, valign=Gtk.Align.END, reveal_child=True,
                                    transition_type=Gtk.RevealerTransitionType.CROSSFADE)
        ov.add_overlay(self.bot_rev)

        # buffering box
        bb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, css_classes=["osd", "buffer-box"],
                     halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        spin = Adw.Spinner() if hasattr(Adw, "Spinner") else Gtk.Spinner(spinning=True)
        spin.set_size_request(40, 40)
        bb.append(spin)
        self.buf_title = Gtk.Label(css_classes=["title-3"])
        self.buf_lbl = Gtk.Label(css_classes=["dim-label"], justify=Gtk.Justification.CENTER, wrap=True,
                                 max_width_chars=50)
        self.buf_bar = Gtk.ProgressBar()
        for w in (self.buf_title, self.buf_lbl, self.buf_bar):
            bb.append(w)
        self.buf_box = bb
        ov.add_overlay(bb)

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        ov.add_controller(motion)
        click = Gtk.GestureClick()
        click.connect("pressed", self._on_click)
        self.video.add_controller(click)
        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", lambda c, dx, dy: (self.vol.set_value(self.vol.get_value() - dy * 5), True)[1])
        self.video.add_controller(scroll)
        self._last_motion = (0, 0)
        return ov

    # ── documentaries ─────────────────────────────────────────────────────
    def _build_doc_shelf(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_start=16, margin_end=16,
                      margin_bottom=10, visible=False)
        head = Gtk.Box(spacing=8)
        self.shelf_lbl = Gtk.Label(label="Streaming now", xalign=0, hexpand=True, css_classes=["title-4"],
                                   margin_start=6)
        head.append(self.shelf_lbl)
        live = Gtk.Button(label="📺 Live documentary channels", css_classes=["pill"])
        live.connect("clicked", lambda *_: self._open_live_docs())
        head.append(live)
        box.append(head)
        self.shelf_row = Gtk.Box(spacing=6)
        hs = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True)
        hs.set_child(self.shelf_row)
        box.append(hs)
        self.doc_shelf = box
        self._shelf_loaded_for = None
        return box

    def _open_live_docs(self):
        self.settings["live_section"] = "documentary"
        self.views.set_visible_child_name("live")
        self.livetv.on_shown()
        if self.livetv.index is not None:
            self.livetv.show_section("documentary")

    def _set_category(self, k):
        if not self.cat_btns[k].get_active():
            self.cat_btns[k].set_active(True)          # its "toggled" handler calls us again
            return
        self.category = k
        self.settings["discover_category"] = k
        save_settings(self.settings)
        self.search.set_placeholder_text("Search free documentaries — history, space, nature, war…" if k == "docs"
                                         else "Search free movies — classics, cartoons, noir, open films…")
        self.run_search(self.search.get_text())

    def _load_doc_shelf(self):
        countries = self.mylist.countries()
        key = tuple(countries)
        if self._shelf_loaded_for == key:
            return
        self._shelf_loaded_for = key
        names = " & ".join({"ZA": "South Africa", "AE": "UAE"}[c] for c in countries)
        self.shelf_lbl.set_label(f"Streaming now in {names} · loading…")

        def work():
            try:
                res = streaming_docs(countries)
            except Exception:
                res = None
            GLib.idle_add(done, res)

        def done(res):
            while (c := self.shelf_row.get_first_child()):
                self.shelf_row.remove(c)
            if not res:
                self.shelf_lbl.set_label(f"Streaming now in {names} · couldn't load (offline?)")
                self._shelf_loaded_for = None
                return
            self.shelf_lbl.set_label(f"Documentaries streaming now in {names}")
            for t in res:
                self.shelf_row.append(StreamCard(self.mylist, t))
        threading.Thread(target=work, daemon=True).start()

    # ── search ────────────────────────────────────────────────────────────
    def run_search(self, text):
        self.search_gen += 1
        gen = self.search_gen
        cat = getattr(self, "category", "movies")
        self.disc_stack.set_visible_child_name("loading")
        noun = "documentaries" if cat == "docs" else "movies"
        self.status_lbl.set_label("Searching…" if text else f"Popular free {noun}")
        docs_home = cat == "docs" and not text.strip()
        self.doc_shelf.set_visible(docs_home)
        self.free_lbl.set_visible(docs_home)
        self.free_lbl.set_label("Free to watch right here – public domain")
        if docs_home and hasattr(self, "mylist"):
            self._load_doc_shelf()

        def work():
            try:
                res = catalog.search(text, category=cat)
                GLib.idle_add(self._show_results, gen, text, res, None)
            except Exception as e:
                GLib.idle_add(self._show_results, gen, text, None, e)
        threading.Thread(target=work, daemon=True).start()

    def _show_results(self, gen, text, res, err):
        if gen != self.search_gen:
            return
        if err:
            self.disc_stack.set_visible_child_name("error")
            self.status_lbl.set_label("")
            return
        self._last_results = (text, res)
        show_unv = self.settings.get("show_unverified", False)
        shown = [r for r in res if r["verdict"] != catalog.BLOCKED and (show_unv or r["verdict"] != catalog.UNVERIFIED)]
        if self.settings.get("english_only", True):
            shown = [r for r in shown if r.get("english") is not False]
        hidden_blocked = sum(1 for r in res if r["verdict"] == catalog.BLOCKED)
        hidden_unv = sum(1 for r in res if r["verdict"] == catalog.UNVERIFIED) if not show_unv else 0
        self.flow.remove_all()
        for r in shown:
            self.flow.append(MovieCard(self, r))
        noun = "documentaries" if getattr(self, "category", "movies") == "docs" else "movies"
        bits = [f"{len(shown)} free {noun}" if text else f"Popular free {noun}"]
        if hidden_blocked:
            bits.append(f"{hidden_blocked} unauthorised uploads hidden")
        if hidden_unv:
            bits.append(f"{hidden_unv} unverified hidden")
        self.status_lbl.set_label(" · ".join(bits))
        self.disc_stack.set_visible_child_name("results" if shown else "empty")

    def _toggle_setting(self, action, value, key):
        action.set_state(value)
        self.settings[key] = value.get_boolean()
        save_settings(self.settings)
        if key == "english_only" and getattr(self, "_last_results", None):
            self._show_results(self.search_gen, *self._last_results, None)

    def _toggle_unverified(self, action, value):
        action.set_state(value)
        self.settings["show_unverified"] = value.get_boolean()
        save_settings(self.settings)
        if getattr(self, "_last_results", None):
            self._show_results(self.search_gen, *self._last_results, None)

    def load_thumb(self, url, pic):
        if url in self.thumb_cache:
            pic.set_paintable(self.thumb_cache[url])
            return

        def work():
            try:
                data = fetch(url, timeout=15)
                GLib.idle_add(done, data)
            except Exception:
                pass

        def done(data):
            try:
                tex = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
                self.thumb_cache[url] = tex
                pic.set_paintable(tex)
            except GLib.Error:
                pass
        self.thumbs.submit(work)

    # ── details dialog ───────────────────────────────────────────────────
    def show_details(self, r):
        d = Adw.Dialog(title=r["title"], content_width=620)
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        box = Gtk.Box(spacing=18, margin_start=24, margin_end=24, margin_bottom=24, margin_top=6)
        pic = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, css_classes=["poster"], valign=Gtk.Align.START)
        pic.set_overflow(Gtk.Overflow.HIDDEN)
        pic.set_size_request(180, 260)
        self.load_thumb(r["thumb"], pic)
        box.append(pic)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, hexpand=True)
        col.append(Gtk.Label(label=r["title"], wrap=True, xalign=0, css_classes=["hero-title"]))
        meta = [str(r["year"]) if r["year"] else None, r["runtime"],
                f"★ {r['rating']}" if r["rating"] else None, f"{r['downloads']:,} views"]
        col.append(Gtk.Label(label=" · ".join(m for m in meta if m), xalign=0, css_classes=["dim-label"]))
        text, cls = BADGE[r["verdict"]]
        badge_row = Gtk.Box(spacing=8)
        badge_row.append(Gtk.Label(label=text, css_classes=["badge", cls]))
        badge_row.append(Gtk.Label(label=r["reason"], css_classes=["caption", "dim-label"], wrap=True, xalign=0))
        col.append(badge_row)
        if r["description"]:
            col.append(Gtk.Label(label=r["description"], wrap=True, xalign=0, lines=8,
                                 ellipsize=Pango.EllipsizeMode.END, selectable=False))
        btns = Gtk.Box(spacing=8, margin_top=8)
        watch = Gtk.Button(label="▶  Watch now", css_classes=["suggested-action", "pill"])
        watch.connect("clicked", lambda *_: (d.close(), self.watch_ia(r)))
        dl = Gtk.Button(label="Download only", css_classes=["pill"])
        dl.connect("clicked", lambda *_: (d.close(), self.watch_ia(r, play=False)))
        btns.append(watch)
        btns.append(dl)
        col.append(btns)
        box.append(col)
        sc = Gtk.ScrolledWindow(propagate_natural_height=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        sc.set_child(box)
        tv.set_content(sc)
        d.set_child(tv)
        d.present(self)
        watch.grab_focus()

    # ── starting downloads ────────────────────────────────────────────────
    def watch_ia(self, r, play=True):
        if play:
            self.enter_player(r["title"], None)
            self.set_buffering("Checking authenticity…", "Confirming rights and fetching the original file list", None)
        else:
            self.toast(f"Adding “{r['title']}”…")

        def work():
            try:
                info = catalog.resolve(r["id"])
                ok, problems, _warn = safety.check_files([(info["file_name"], info["size"])])
                if not ok:
                    raise PermissionError("; ".join(problems))
                meta = {"title": info["title"], "source": "archive.org", "ia_id": r["id"],
                        "file_name": info["file_name"], "sha1": info["sha1"], "trust": info["verdict"],
                        "reason": info["reason"], "subtitles": info["subtitles"], "added": time.time()}
                it = self.engine.add_torrent_url(info["torrent_url"], meta)
                GLib.idle_add(self._item_added, it, play)
            except Exception as e:
                GLib.idle_add(self._start_failed, e, play)
        threading.Thread(target=work, daemon=True).start()

    def _start_failed(self, e, play):
        if play:
            self.leave_player()
        msg = str(e) if isinstance(e, (PermissionError, RuntimeError)) else f"Network problem: {e}"
        dlg = Adw.AlertDialog(heading="Can't play this title", body=msg)
        dlg.add_response("ok", "OK")
        dlg.present(self)

    def _item_added(self, it, play):
        self._add_row(it)
        if play:
            self.watch_item(it)
        else:
            it.resume()
            self.toast(f"Downloading “{it.title}” – see Library")

    def add_magnet(self, uri, play=True):
        try:
            it = self.engine.add_magnet(uri, {"trust": "magnet", "added": time.time()})
        except Exception as e:
            self._start_failed(RuntimeError(f"That magnet link couldn't be opened ({e})."), False)
            return
        self._add_row(it)
        if play:
            self.watch_item(it)

    def add_torrent_file(self, path, play=True):
        try:
            with open(path, "rb") as f:
                data = f.read()
            it = self.engine.add_torrent_bytes(data, {"trust": "magnet", "added": time.time()})
        except Exception as e:
            self._start_failed(RuntimeError(f"That .torrent file couldn't be read ({e})."), False)
            return
        self._add_row(it)
        if play:
            self.watch_item(it)

    def watch_item(self, it):
        self.enter_player(it.title, it)
        if it.is_complete() and it.file_index is not None:
            self._play_now(it)
            return
        self.set_buffering("Connecting…", "Finding peers", 0)
        it.resume()
        self.pending_play = it

    def _safety_gate(self, it):
        """Run on user-supplied torrents once their file list is known."""
        if it.meta.get("safety_ok") or it.meta.get("source") == "archive.org":
            return True
        ok, problems, warnings = safety.check_files(it.all_files())
        if not ok:
            self.pending_play = None
            if self.current is it:
                self.current = None
                self.leave_player()
            dlg = Adw.AlertDialog(heading="This torrent looks fake or dangerous",
                                  body="\n".join("• " + p for p in problems) +
                                       "\n\nIt has been removed and nothing was opened.")
            dlg.add_response("ok", "OK")
            dlg.present(self)
            self.engine.remove(it, delete_files=True)
            self._remove_row(it)
            return False
        if warnings and not it.meta.get("warn_ack"):
            self.pending_play = None
            dlg = Adw.AlertDialog(heading="Be careful with this torrent",
                                  body="\n".join("• " + w for w in warnings) + "\n\nPlay it anyway?")
            dlg.add_response("remove", "Remove it")
            dlg.add_response("play", "Play anyway")
            dlg.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
            dlg.set_default_response("remove")

            def resp(_d, r):
                if r == "play":
                    it.meta["warn_ack"] = True
                    it.meta["safety_ok"] = True
                    self.engine.save_state()
                    self.pending_play = it
                else:
                    if self.current is it:
                        self.current = None
                        self.leave_player()
                    self.engine.remove(it, delete_files=True)
                    self._remove_row(it)
            dlg.connect("response", resp)
            dlg.present(self)
            return False
        it.meta["safety_ok"] = True
        self.engine.save_state()
        return True

    def _play_now(self, it):
        self.pending_play = None
        self._remember_position()
        start = None
        if it.is_complete():
            path = it.file_info()[0]
            url = path
            self.local_path = path
            start = (self.settings.get("resume", {}).get(path) or {}).get("t")
        else:
            url = it.stream_url()
            self.local_path = None
        subs = list(it.subtitle_paths()) + list(it.meta.get("subtitles") or [])[:3]
        self.set_buffering("Starting…", "", None)
        self._reset_live()
        self._stop_translation()
        self._lang_done = False
        self.doctor.start("local" if it.is_complete() else "torrent", url, it.title, item=it)
        self.video.play(url, subs, start=start)
        self._waiting_first_frame = time.time()

    # ── player control ───────────────────────────────────────────────────
    def enter_player(self, title, item):
        self.current = item
        self.p_title.set_label(title)
        trust = (item.meta.get("trust") if item else None)
        lbl = {"verified": "✔ Verified", "likely": "✔ Free to share"}.get(trust)
        self.p_trust.set_visible(bool(lbl))
        self.p_trust.set_label(lbl or "")
        self.seek.set_value(0)
        self.seek.set_fill_level(0)
        self.time_lbl.set_label("0:00 / --:--")
        self.dl_lbl.set_label("")
        self.root.set_visible_child_name("player")
        self.video.mpv.volume = self.vol.get_value()
        self._show_controls()

    def leave_player(self):
        self.doctor.stop()
        self._remember_position()
        self.local_path = None
        self.pending_play = None
        self._stop_translation()
        self.video.stop()
        if self.is_fullscreen():
            self.unfullscreen()
        self.root.set_visible_child_name("browse")
        self.set_cursor(None)
        if self.current and not self.current.is_complete():
            self.toast("Download continues in the Library")
        self.current = None

    def set_buffering(self, title, text, frac):
        if title is None:
            self.buf_box.set_visible(False)
            return
        self.buf_box.set_visible(True)
        self.buf_title.set_label(title)
        self.buf_lbl.set_label(text)
        self.buf_bar.set_visible(frac is not None)
        if frac is not None:
            self.buf_bar.set_fraction(frac)

    def toggle_pause(self):
        self.video.mpv.pause = not self.video.mpv.pause
        self._show_controls()

    def seek_rel(self, s):
        try:
            self.video.mpv.seek(s, "relative")
        except Exception:
            pass
        self._show_controls()

    def _on_seek(self, _scale, _scroll, value):
        try:
            self.video.mpv.seek(max(0, value), "absolute")
        except Exception:
            pass
        return False

    def _on_volume(self, _b, v):
        self.video.mpv.volume = v
        self.settings["volume"] = v

    def toggle_fullscreen(self):
        self.unfullscreen() if self.is_fullscreen() else self.fullscreen()

    def _build_tracks_popover(self, mb):
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=8, margin_bottom=8,
                      margin_start=8, margin_end=8)
        tl = self.video.mpv.track_list or []
        for kind, label, prop in (("sub", "Subtitles", "sid"), ("audio", "Audio", "aid")):
            box.append(Gtk.Label(label=label, xalign=0, css_classes=["heading"], margin_top=4))
            group = None
            opts = [(False, "Off")] if kind == "sub" else []
            for t in tl:
                if t.get("type") == kind:
                    name = t.get("title") or t.get("lang") or f"Track {t['id']}"
                    if t.get("lang") and t.get("title"):
                        name = f"{t['title']} ({t['lang']})"
                    opts.append((t["id"], name))
            if not opts:
                box.append(Gtk.Label(label="None", xalign=0, css_classes=["dim-label"]))
            cur = getattr(self.video.mpv, prop)
            if kind == "sub":
                for t in tl:
                    if (t.get("type") == "sub" and not translate.is_english(t.get("lang"))
                            and t.get("codec") in translate.TEXT_SUB_CODECS):
                        name = t.get("lang") or t.get("title") or f"Track {t['id']}"
                        opts.insert(1, (("tr", t["id"]), f"English – translated from {name}"))
                if self.tr_track is not None:
                    cur = ("tr", self.tr_track)
            for val, name in opts:
                active = cur == val or (val is False and not cur)
                cb = Gtk.CheckButton(label=name, group=group, active=active)
                group = group or cb
                cb.connect("toggled", lambda b, p=prop, v=val: b.get_active() and self._select_track(p, v))
                box.append(cb)
        add = Gtk.Button(label="Load subtitle file…", css_classes=["flat"], margin_top=6)
        add.connect("clicked", lambda *_: (pop.popdown(), self.pick_subtitle()))
        box.append(add)
        pop.set_child(box)
        mb.set_popover(pop)

    def _reset_live(self):
        self.live = False
        self.seek.set_visible(True)
        m = self.video.mpv
        m.user_agent = "CineHaven"
        m.referrer = ""
        m.demuxer_lavf_analyzeduration = 0
        m.demuxer_lavf_o = ""
        try:
            m["hls-bitrate"] = "max"
        except Exception:
            pass

    def play_live(self, ch, stream):
        self._remember_position()
        self.local_path = None
        self.enter_player(ch["name"], None)
        self.p_trust.set_label("● LIVE")
        self.p_trust.set_visible(True)
        self.pending_play = None
        self._stop_translation()
        self.live = True
        self._live_t = time.time()
        self.seek.set_visible(False)
        m = self.video.mpv
        m.user_agent = stream.get("ua") or "Mozilla/5.0 (X11; Linux x86_64)"
        m.referrer = stream.get("ref") or ""
        m.demuxer_lavf_analyzeduration = 1          # start live streams faster
        m.demuxer_lavf_o = "live_start_index=-3"
        try:
            m["hls-bitrate"] = 3_000_000                  # ≤ ~720p: smooth on ordinary connections
        except Exception:
            pass
        self.set_buffering("Tuning in…", f"{ch['name']} – live channels take about 10–20 seconds to start", None)
        self._lang_done = False
        idx = ch["streams"].index(stream) if stream in ch["streams"] else 0
        self.doctor.start("live", stream["url"], ch["name"], ch=ch, stream_idx=idx)
        self.video.play(stream["url"])

    def _select_track(self, prop, val):
        if prop == "sid":
            if isinstance(val, tuple):
                self._start_translation(val[1])
                return
            self._stop_translation()
        setattr(self.video.mpv, prop, val)

    # ── English audio / subtitles / live translation ─────────────────────
    def _auto_language(self):
        m = self.video.mpv
        tl = m.track_list or []
        auds = [t for t in tl if t.get("type") == "audio"]
        subs = [t for t in tl if t.get("type") == "sub"]
        cur_a = next((t for t in auds if t.get("selected")), None)
        alang = cur_a.get("lang") if cur_a else None
        eng_audio = next((t for t in auds if translate.is_english(t.get("lang"))), None)
        if eng_audio and not translate.is_english(alang):
            m.aid = eng_audio["id"]
            self.toast("Switched to the English soundtrack")
            return
        if alang is None or translate.is_english(alang):
            return                     # English (or unlabelled) audio – no subtitles needed
        eng_sub = next((t for t in subs if translate.is_english(t.get("lang"))), None)
        if eng_sub:
            m.sid = eng_sub["id"]
            self.toast("English subtitles on")
            return
        text_subs = [t for t in subs if t.get("codec") in translate.TEXT_SUB_CODECS]
        if text_subs:
            self._start_translation(text_subs[0]["id"])
        else:
            self.toast("No English audio or subtitles in this file")

    def _start_translation(self, track_id):
        m = self.video.mpv
        self._stop_translation()
        m.sid = track_id
        m.sub_visibility = False
        self.tr_track = track_id
        m.observe_property("sub-text", self._on_sub_text)
        lang = next((t.get("lang") for t in (m.track_list or []) if t.get("id") == track_id
                     and t.get("type") == "sub"), None)
        self.toast(f"Translating {lang or 'subtitles'} to English live")

    def _stop_translation(self):
        if self.tr_track is None:
            return
        m = self.video.mpv
        self.tr_track = None
        self._sub_text = ""
        try:
            m.unobserve_property("sub-text", self._on_sub_text)
            m.sub_visibility = True
            m.command("osd-overlay", 63, "none", "")
        except Exception:
            pass

    def _on_sub_text(self, _name, value):       # runs on mpv's event thread
        GLib.idle_add(self._sub_changed, value or "")

    def _sub_changed(self, text):
        if self.tr_track is None:
            return False
        self._sub_text = text
        if not text.strip():
            self.video.mpv.command("osd-overlay", 63, "none", "")
            return False

        def work():
            en = translate.to_english(text)
            GLib.idle_add(self._render_sub, text, en)
        self.tr_pool.submit(work)
        return False

    def _render_sub(self, src, en):
        if self.tr_track is None or src != self._sub_text:
            return False
        ass = "{\\an2\\fs46\\bord2.5\\shad1}" + translate.ass_escape(en)
        try:
            self.video.mpv.command("osd-overlay", 63, "ass-events", ass, 0, 720, 0, "no", "no")
        except Exception:
            pass
        return False

    def _show_controls(self):
        self.top_rev.set_reveal_child(True)
        self.bot_rev.set_reveal_child(True)
        self.set_cursor(None)
        if self.hide_src:
            GLib.source_remove(self.hide_src)
        self.hide_src = GLib.timeout_add(2800, self._auto_hide)

    def _auto_hide(self):
        self.hide_src = None
        if self.root.get_visible_child_name() != "player":
            return False
        if self.video.mpv.pause or self.buf_box.get_visible():
            return False
        self.top_rev.set_reveal_child(False)
        self.bot_rev.set_reveal_child(False)
        self.set_cursor(Gdk.Cursor.new_from_name("none"))
        return False

    def _on_motion(self, _c, x, y):
        if abs(x - self._last_motion[0]) + abs(y - self._last_motion[1]) > 2:
            self._last_motion = (x, y)
            self._show_controls()

    def _on_click(self, gesture, n, _x, _y):
        if n == 2:
            self.toggle_fullscreen()
        self._show_controls()

    def _on_key(self, _c, keyval, _code, state):
        if self.root.get_visible_child_name() != "player":
            if keyval == Gdk.KEY_f and state & Gdk.ModifierType.CONTROL_MASK:
                self.views.set_visible_child_name("discover")
                self.search.grab_focus()
                return True
            return False
        k = Gdk.keyval_name(keyval)
        m = self.video.mpv
        acts = {
            "space": self.toggle_pause, "k": self.toggle_pause, "p": self.toggle_pause,
            "f": self.toggle_fullscreen, "F11": self.toggle_fullscreen,
            "Left": lambda: self.seek_rel(-10), "Right": lambda: self.seek_rel(30),
            "j": lambda: self.seek_rel(-10), "l": lambda: self.seek_rel(10),
            "Up": lambda: self.vol.set_value(self.vol.get_value() + 5),
            "Down": lambda: self.vol.set_value(self.vol.get_value() - 5),
            "m": lambda: setattr(m, "mute", not m.mute),
            "s": lambda: m.cycle("sub"), "v": lambda: m.cycle("sub"),
            "a": lambda: m.cycle("audio"), "b": lambda: m.cycle("audio"),
            "bracketleft": lambda: setattr(m, "speed", max(0.25, m.speed - 0.1)),
            "bracketright": lambda: setattr(m, "speed", min(4, m.speed + 0.1)),
            "BackSpace": lambda: setattr(m, "speed", 1.0),
        }
        if k == "Escape":
            self.unfullscreen() if self.is_fullscreen() else self.leave_player()
            return True
        if k in acts:
            try:
                acts[k]()
            except Exception:
                pass
            self._show_controls()
            return True
        return False

    def _player_tick(self):
        if self.root.get_visible_child_name() != "player":
            return True
        m = self.video.mpv
        try:
            pos, dur = m.time_pos, m.duration
            paused, cache_wait = m.pause, m.paused_for_cache
        except Exception:
            return True
        if self.pending_play is None:
            self.doctor.tick(pos, paused, cache_wait)
        self.pp.set_icon_name("media-playback-start-symbolic" if paused else "media-playback-pause-symbolic")
        if paused and not self.bot_rev.get_reveal_child():
            self._show_controls()
        if dur:
            self.seek.set_range(0, dur)
            if pos is not None:
                self.seek.set_value(pos)
        if self.live:
            self.time_lbl.set_label("● LIVE")
            if pos is None and time.time() - self._live_t > 45:
                self.set_buffering("This channel isn't responding", "It may be off air right now – try another channel.", None)
        else:
            self.time_lbl.set_label(f"{fmt_time(pos or 0)} / {fmt_time(dur)}")
        if pos is not None and not self._lang_done and pos > 0.5:
            self._lang_done = True
            if self.settings.get("auto_english", True):
                self._auto_language()
        if self.pending_play is None and pos is not None:
            if cache_wait:
                st = self.current.handle.status() if self.current else None
                rate = f"↓ {human_bytes(st.download_rate)}/s · {st.num_peers} peers" if st else ""
                self.set_buffering("Buffering…", rate, None)
            else:
                self.set_buffering(None, None, None)
        return True

    # ── periodic: torrents ───────────────────────────────────────────────
    def _tick(self):
        for it, ev in self.engine.poll_alerts():
            if ev == "metadata":
                if it.file_index is None:
                    it.choose_file()
                    if not self._safety_gate(it):
                        continue
            elif ev == "finished":
                self._on_finished(it)
            elif ev.startswith("error:"):
                self.toast(f"{it.title}: {ev[6:]}")
        for it in list(self.engine.items.values()):          # finish Archive downloads
            if it.key not in self.verifying and it.has_metadata() and it.file_index is not None \
                    and time.time() > getattr(it, "_patch_next", 0) and it.needs_patch():
                self._patch(it)
        it = self.pending_play
        if it is not None and it.key in self.engine.items:
            st = it.handle.status()
            if not it.has_metadata():
                self.set_buffering("Connecting…", f"Finding peers · {st.num_peers} connected", None)
            elif it.file_index is None:
                it.choose_file()
                self._safety_gate(it)   # no video → reported + removed by the gate
            else:
                if not getattr(it, "streaming", False) and self._safety_gate(it):
                    it.streaming = True
                    it.stream_t = time.time()
                    it.start_streaming()
                if self.pending_play is it and getattr(it, "streaming", False):
                    p = it.prebuffer_progress()
                    self.set_buffering("Buffering…", f"↓ {human_bytes(st.download_rate)}/s · "
                                       f"{st.num_peers} peers · playback starts automatically", p)
                    slow = time.time() - getattr(it, "stream_t", time.time()) > 30
                    if p >= 1.0 or (slow and it.http_url):   # peers slow → Archive fills the gaps
                        if p < 1.0:
                            health.log.info("Slow start for %s – playing with Archive backup", it.title)
                        self._play_now(it)
        # player download info + seek-bar fill
        cur = self.current
        if cur and self.root.get_visible_child_name() == "player" and cur.has_metadata() and cur.file_index is not None:
            st = cur.handle.status()
            frac = cur.file_progress()
            if frac >= 1:
                self.dl_lbl.set_label("Downloaded ✔")
            else:
                self.dl_lbl.set_label(f"Downloaded {frac*100:.0f}% · ↓ {human_bytes(st.download_rate)}/s")
            dur = self.video.mpv.duration
            if dur:
                self.seek.set_fill_level(self._contiguous_fraction(cur) * dur)
        for row in self.rows.values():
            row.refresh()
        return True

    def _contiguous_fraction(self, it):
        """How far from the start of the file is downloaded without gaps."""
        if it.is_complete():
            return 1.0
        _, off, size, plen, _ = it.file_info()
        first, last = off // plen, (off + size - 1) // plen
        pos = self.video.mpv.time_pos or 0
        dur = self.video.mpv.duration or 1
        p = first + int((last - first) * pos / dur)
        h = it.handle
        while p <= last and h.have_piece(p):
            p += 1
        return min(1.0, max(0.0, ((p - first) * plen) / size))

    def finish_from_archive(self, it):
        """Download doctor: swarm stalled – fetch everything still missing from archive.org."""
        self.toast(f"“{it.title}” stalled – finishing it directly from the Internet Archive")
        self._patch(it, all_missing=True)

    def _patch(self, it, all_missing=False):
        self.verifying.add(it.key)

        def work():
            try:
                ok = it.patch_from_archive(safety.sha1_file, all_missing=all_missing)
            except Exception as e:
                health.log.warning("Archive fill failed for %s: %s", it.title, e)
                ok = None
            GLib.idle_add(done, ok)

        def done(ok):
            self.verifying.discard(it.key)
            if ok:
                it.meta["finished_notified"] = True
                self.engine.save_state()
                self.app.notify(f"“{it.title}” finished downloading")
                self.toast(f"✔ “{it.title}” downloaded – verified identical to the original")
            elif ok is False:
                self.toast(f"⚠ “{it.title}” doesn't match the Archive's checksum")
            if not ok:
                it._patch_next = time.time() + 120
            return False
        threading.Thread(target=work, daemon=True).start()

    def _on_finished(self, it):
        if it.meta.get("finished_notified") or not it.is_complete():
            return
        it.meta["finished_notified"] = True
        self.engine.save_state()
        self.app.notify(f"“{it.title}” finished downloading")
        sha1 = it.meta.get("sha1")
        if sha1 and it.verified is None and it.key not in self.verifying:
            self.verifying.add(it.key)
            path = it.file_info()[0]

            def work():
                try:
                    ok = safety.sha1_file(path, sha1)
                except OSError:
                    ok = None
                GLib.idle_add(done, ok)

            def done(ok):
                self.verifying.discard(it.key)
                if ok is None:
                    return
                it.verified = ok
                it.meta["verified"] = ok
                self.engine.save_state()
                self.toast("✔ Verified: identical to the original" if ok
                           else "⚠ File differs from the Archive's listed checksum")
            threading.Thread(target=work, daemon=True).start()

    # ── library rows ──────────────────────────────────────────────────────
    def _add_row(self, it):
        if it.key in self.rows:
            return
        row = LibraryRow(self, it)
        self.rows[it.key] = row
        self.listbox.prepend(row)
        self._update_lib_empty()

    def _remove_row(self, it):
        row = self.rows.pop(it.key, None)
        if row:
            self.listbox.remove(row)
        self._update_lib_empty()

    def _update_lib_empty(self):
        self.lib_stack.set_visible_child_name("list" if self.rows else "empty")
        n = sum(1 for r in self.rows.values() if not r.item.is_complete())
        self.lib_page.set_badge_number(n)
        self.lib_page.set_needs_attention(False)

    def confirm_remove(self, it):
        dlg = Adw.AlertDialog(heading=f"Remove “{it.title}”?",
                              body="Remove it from the library. You can also delete the downloaded file.")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("keep", "Remove, keep file")
        dlg.add_response("delete", "Delete file")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_close_response("cancel")

        def resp(_d, r):
            if r in ("keep", "delete"):
                if self.current is it:
                    self.leave_player()
                self.engine.remove(it, delete_files=(r == "delete"))
                self._remove_row(it)
        dlg.connect("response", resp)
        dlg.present(self)

    def open_folder(self, it):
        try:
            path = it.file_info()[0] if it.file_index is not None else DOWNLOAD_DIR
            f = Gio.File.new_for_path(path if os.path.exists(path) else os.path.dirname(path))
            Gtk.FileLauncher.new(f).open_containing_folder(self, None, None)
        except Exception:
            Gio.AppInfo.launch_default_for_uri(GLib.filename_to_uri(DOWNLOAD_DIR), None)

    # ── open dialogs ─────────────────────────────────────────────────────
    def ask_magnet(self, *_):
        dlg = Adw.AlertDialog(heading="Paste a magnet link",
                              body="Only add links to content you have the right to download. "
                                   f"{APP_NAME} checks the files for fakes and malware before playing.")
        entry = Gtk.Entry(placeholder_text="magnet:?xt=urn:btih:…", activates_default=True)
        cb = Gdk.Display.get_default().get_clipboard()

        def got(c, res):
            try:
                t = c.read_text_finish(res) or ""
                if normalize_magnet(t):
                    entry.set_text(normalize_magnet(t))
            except GLib.Error:
                pass
        cb.read_text_async(None, got)
        dlg.set_extra_child(entry)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("watch", "Watch")
        dlg.set_response_appearance("watch", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("watch")
        def resp(_d, r):
            if r != "watch":
                return
            if normalize_magnet(entry.get_text()):
                self.add_magnet(entry.get_text())
            else:
                self._start_failed(RuntimeError("That doesn't look like a magnet link. It should start with "
                                                "“magnet:?xt=urn:btih:”."), False)
        dlg.connect("response", resp)
        dlg.present(self)

    def _pick(self, title, patterns, cb):
        fd = Gtk.FileDialog(title=title)
        flt = Gtk.FileFilter()
        flt.set_name(title)
        for p in patterns:
            if "/" in p:
                flt.add_mime_type(p)
            else:
                flt.add_pattern(p)
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(flt)
        fd.set_filters(store)

        def done(d, res):
            try:
                f = d.open_finish(res)
                if f:
                    cb(f.get_path())
            except GLib.Error:
                pass
        fd.open(self, None, done)

    def pick_torrent(self, *_):
        self._pick("Torrent files", ["*.torrent", "application/x-bittorrent"], self.add_torrent_file)

    def pick_video(self, *_):
        self._pick("Video files", ["video/*", "*.mkv", "*.mp4", "*.avi", "*.webm"], self.play_local)

    def pick_subtitle(self):
        self._pick("Subtitles", ["*.srt", "*.vtt", "*.ass", "*.ssa", "*.sub"],
                   lambda p: self.video.mpv.command("sub-add", p, "select"))

    def play_local(self, path, title=None):
        self._remember_position()
        self.enter_player(title or os.path.basename(path), None)
        self.set_buffering(None, None, None)
        self.pending_play = None
        self._reset_live()
        self._stop_translation()
        self._lang_done = False
        self.local_path = path
        start = (self.settings.get("resume", {}).get(path) or {}).get("t")
        if start:
            self.toast(f"Resuming from {fmt_time(start)} – right-click the video for “Play from the start”")
        self.doctor.start("local", path, title or os.path.basename(path))
        self.video.play(path, start=start)

    def _remember_position(self):
        """Save where the user stopped so the video resumes there next time."""
        path = self.local_path
        if not path:
            return
        try:
            pos, dur = self.video.mpv.time_pos, self.video.mpv.duration
        except Exception:
            return
        res = self.settings.setdefault("resume", {})
        if pos and dur and 60 < pos < dur - 180:
            res[path] = {"t": round(pos, 1), "dur": round(dur, 1), "when": time.time()}
        elif pos and dur and pos >= dur - 180:
            res.pop(path, None)           # finished – start from the beginning next time
        if len(res) > 500:                # keep the newest 500
            for k in sorted(res, key=lambda k: res[k].get("when", 0))[:-500]:
                res.pop(k)
        save_settings(self.settings)

    def forget_resume(self, path):
        self.settings.get("resume", {}).pop(path, None)
        save_settings(self.settings)

    def open_any(self, arg):
        if normalize_magnet(arg) and not os.path.exists(arg):
            self.add_magnet(arg)
        elif arg.lower().endswith(".torrent"):
            self.add_torrent_file(arg)
        elif os.path.exists(arg):
            self.play_local(arg)

    def _on_drop(self, _t, value, _x, _y):
        for f in value.get_files():
            p = f.get_path()
            if p:
                self.open_any(p)
        return True

    def show_about(self, *_):
        about = Adw.AboutDialog(
            application_name=APP_NAME, application_icon=APP_ID, version=APP_VERSION,
            developer_name="CineHaven contributors", license_type=Gtk.License.GPL_3_0,
            comments="Search, stream and download free movies.\n\nStarts playing while it downloads, "
                     "checks every title's rights and every file's integrity, and plays any "
                     "video format thanks to mpv/FFmpeg.\n\nCatalogue: Internet Archive moving-image library.",
            website="https://archive.org/details/movies")
        about.present(self)

    def _report_selfcheck(self):
        if health.REPAIRS:
            self.toast(f"CineHaven fixed {len(health.REPAIRS)} problem(s) on startup")
        for w in health.WARNINGS:
            self.toast("⚠ " + w)
        if health.SAFE_MODE:
            self.toast("Running in safe mode after a crash – hardware video decoding is off")
        return False

    def check_updates(self, manual=False):
        from . import UPDATE_REPO, update
        if not UPDATE_REPO:
            if manual:
                self.toast("Updates will be available once CineHaven is published")
            return

        def work():
            try:
                avail = update.check(force=manual)
                installed = False
                if avail and update.user_install() and self.settings.get("auto_update", True):
                    installed = update.install(avail)
                GLib.idle_add(done, avail, installed, None)
            except Exception as e:
                health.log.warning("Update check failed: %s", e)
                GLib.idle_add(done, None, False, e)

        def done(avail, installed, err):
            if err and manual:
                self.toast("Couldn't check for updates right now")
            elif installed:
                self.toast(f"CineHaven {avail['version']} installed – it starts next time you open the app")
            elif avail:
                t = Adw.Toast(title=f"CineHaven {avail['version']} is available", button_label="Get it", timeout=0)
                t.connect("button-clicked", lambda *_: Gtk.UriLauncher.new(avail["url"]).launch(self, None, None))
                self.toasts.add_toast(t)
            elif manual:
                self.toast(f"You have the latest version ({APP_VERSION})")
            return False
        threading.Thread(target=work, daemon=True).start()

    def show_diagnostics(self, *_):
        try:
            with open(health.LOG_FILE, encoding="utf-8", errors="replace") as f:
                tail = f.readlines()[-60:]
        except OSError:
            tail = ["(no log yet)"]
        st = self.engine.session.status() if hasattr(self.engine.session, "status") else None
        summary = [f"{APP_NAME} {APP_VERSION}" + (" – safe mode" if health.SAFE_MODE else ""),
                   f"Downloads: {len(self.engine.items)}  ·  Videos found on drives: {len(self.myvideos.videos)}",
                   f"Repairs this session: {len(health.REPAIRS)}  ·  Log: {health.LOG_FILE}"]
        d = Adw.Dialog(title="Help & diagnostics", content_width=760, content_height=560)
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_start=18, margin_end=18,
                      margin_bottom=18)
        box.append(Gtk.Label(label="CineHaven repairs most problems by itself: it retries playback with "
                                   "other settings, switches sources, restarts stuck downloads and restores "
                                   "damaged settings. This is its record of what happened.",
                             wrap=True, xalign=0, css_classes=["dim-label"]))
        box.append(Gtk.Label(label="\n".join(summary), xalign=0, selectable=True))
        view = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        view.get_buffer().set_text("".join(tail))
        sc = Gtk.ScrolledWindow(vexpand=True)
        sc.set_child(view)
        box.append(sc)
        row = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        copy = Gtk.Button(label="Copy report", css_classes=["pill"])
        copy.connect("clicked", lambda *_: (Gdk.Display.get_default().get_clipboard().set(
            "\n".join(summary) + "\n\n" + "".join(tail)), self.toast("Report copied")))
        folder = Gtk.Button(label="Open log folder", css_classes=["pill"])
        folder.connect("clicked", lambda *_: Gio.AppInfo.launch_default_for_uri(GLib.filename_to_uri(DATA_DIR), None))
        row.append(folder)
        row.append(copy)
        box.append(row)
        tv.set_content(box)
        d.set_child(tv)
        d.present(self)

    def save_settings(self):
        save_settings(self.settings)

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=3))

    def _on_close(self, *_):
        self.cleanup()
        return False

    def cleanup(self):
        if getattr(self, "_cleaned", False):
            return
        self._cleaned = True
        self._remember_position()
        save_settings(self.settings)
        self._stop_translation()
        self.tr_pool.shutdown(wait=False, cancel_futures=True)
        self.livetv.shutdown()
        self.myvideos.shutdown()
        self.video.shutdown()
        self.thumbs.shutdown(wait=False, cancel_futures=True)


# ──────────────────────────────────────────────────────────────────────────
class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        self.engine = None
        self.settings = load_settings()
        GLib.set_application_name(APP_NAME)

    def do_startup(self):
        Adw.Application.do_startup(self)
        css = Gtk.CssProvider()
        data = CSS + MYLIST_CSS + LIVE_CSS + LOCAL_CSS
        css.load_from_data(data, len(data))
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        os.makedirs(DATA_DIR, exist_ok=True)
        health.selfcheck()
        try:
            self.engine = Engine()
        except Exception as e:                 # e.g. port in use / corrupt state – retry clean
            health.log.error("Engine failed to start (%s) – retrying with a fresh session", e)
            self.engine = Engine()
        self.set_accels_for_action("win.open-magnet", ["<Ctrl>l", "<Ctrl>v"])
        self.set_accels_for_action("win.open-video", ["<Ctrl>o"])
        q = Gio.SimpleAction(name="quit")
        q.connect("activate", lambda *_: self.quit())
        self.add_action(q)
        self.set_accels_for_action("app.quit", ["<Ctrl>q"])

    def _win(self):
        w = self.get_active_window()
        if not w:
            w = Window(self)
        w.present()
        return w

    def do_activate(self):
        self._win()

    def do_open(self, files, n, hint):
        w = self._win()
        for f in files:
            uri = f.get_uri()
            if uri.lower().startswith("magnet:"):
                w.open_any(uri)
            elif f.get_path():
                w.open_any(f.get_path())

    def notify(self, text):
        n = Gio.Notification.new(APP_NAME)
        n.set_body(text)
        self.send_notification(None, n)

    def do_shutdown(self):
        for w in self.get_windows():
            if isinstance(w, Window):
                w.cleanup()
        if self.engine:
            self.engine.shutdown()
        Adw.Application.do_shutdown(self)


def main():
    try:
        migrated = health.migrate()
    except OSError:
        migrated = False
    health.setup_logging()
    if migrated:
        health.log.info("Migrated data from OpenReel")
    # Magnet links arrive as raw strings; GApplication wants URIs – it copes with both.
    return App().run(sys.argv)

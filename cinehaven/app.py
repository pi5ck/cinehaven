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
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

from . import APP_ID, APP_NAME, APP_VERSION, DATA_DIR, DOWNLOAD_DIR  # noqa: E402
from . import adapt, ai, cast, catalog, mpris, youtube, enhance, fixvideo, health, recommend, remote, safety, skip, translate, websearch  # noqa: E402
from .doctor import DownloadDoctor, PlaybackDoctor  # noqa: E402
from .engine import Engine, fetch, is_video, normalize_magnet  # noqa: E402
from .player import VideoView  # noqa: E402
from .mylist import CSS as MYLIST_CSS, MyListPage, StreamCard, streaming_docs  # noqa: E402
from .livetv_ui import CSS as LIVE_CSS, LiveTVPage  # noqa: E402
from .cameras_ui import CSS as CAM_CSS, CamerasPage  # noqa: E402
from .localmedia_ui import CSS as LOCAL_CSS, MyVideosPage  # noqa: E402
from .youtube_ui import CSS as YT_CSS, FilmCard, YouTubePage, _Thumb  # noqa: E402
from .together_ui import CSS as TOGETHER_CSS, Together  # noqa: E402

SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
BADGE = {catalog.VERIFIED: ("✔ Verified", "success"),
         catalog.LIKELY_OK: ("✔ Free to share", "accent"),
         catalog.UNVERIFIED: ("⚠ Unverified", "warning")}

CSS = b"""
.nudge { min-width: 0; padding-left: 8px; padding-right: 8px; font-weight: bold; }
.skip-btn { font-weight: bold; font-size: 1.1em; padding: 10px 22px; }
.poster { border-radius: 10px; background: alpha(currentColor, .08); }
.recent-thumb { border-radius: 10px; background: alpha(currentColor, .08); }
.mini-player { border-radius: 14px; box-shadow: 0 8px 28px rgba(0,0,0,.45); }
.mini-bar { background: linear-gradient(rgba(0,0,0,.55), rgba(0,0,0,.15) 40%, rgba(0,0,0,.55)); color: white; }
.mini-pp { min-width: 48px; min-height: 48px; }
.mini-prog trough, .mini-prog progress { min-height: 3px; border-radius: 0; }
.hdr-badge { background: linear-gradient(90deg, #f5a623, #e2367a); color: white; font-weight: 800; }
.seek-preview > contents { padding: 4px; }
.recent-progress { margin: 0 8px 6px 8px; }
.recent-progress trough, .recent-progress progress { min-height: 4px; border-radius: 2px; }
.recent-x { min-width: 24px; min-height: 24px; padding: 0; opacity: .75; }
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
        rs = self.win.settings.get("resume", {}).get(f"item:{it.key}")
        if rs:
            state += f" · ▶ resume at {fmt_time(rs['t'])}"
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
        self.local_path = None        # file on disk being played
        self.resume_key = None        # what the current position is saved under
        self._resume_cd = None        # countdown source of the resume prompt
        self._last_pos_save = 0

        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)
        # The player floats above the browse pages, so it can shrink into a corner (picture-in-picture)
        # without the video ever being torn down.
        self.root = _Modes(self)
        self.mini = None                                     # None | "corner" | "window"
        self._full_size = None
        self.together_media = None                           # what's playing, as friends' CineHaven can open it
        self.together = Together(self)
        self._browse_w = self._build_browse()
        self._player_w = self._build_player()
        self.stage = Gtk.Overlay()
        self.stage.set_child(self._browse_w)
        self.stage.add_overlay(self._player_w)
        self.stage.set_measure_overlay(self._player_w, True)
        self.toasts.set_child(self.stage)
        self._set_mode("browse")

        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        self.add_controller(drop)
        tdrop = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.COPY)   # a link dragged from the browser
        tdrop.connect("drop", lambda _t, v, *_: (youtube.is_link(v.strip()) or normalize_magnet(v.strip()))
                      and (self.open_any(v.strip()) or True))
        self.add_controller(tdrop)

        self.remote = remote.Remote(lambda c, v: GLib.idle_add(self._remote_command, c, v))
        self.mpris = mpris.MPRIS(self, APP_ID)              # media keys, headset buttons, desktop media panel
        GLib.timeout_add(500, self._mpris_tick)
        self._sleep = None                                   # sleep timer: ("at", time) or ("end", None)
        self.doctor = PlaybackDoctor(self)
        self.dl_doctor = DownloadDoctor(self)
        threading.Thread(target=fixvideo.cleanup, daemon=True).start()
        GLib.timeout_add(1000, self._tick)
        GLib.timeout_add(2500, self._report_selfcheck)
        GLib.timeout_add_seconds(30, lambda: (self.check_updates(), False)[1])
        GLib.timeout_add_seconds(20, lambda: (self._start_probe(), False)[1])
        GLib.timeout_add_seconds(45, lambda: (youtube.update_in_background(), False)[1])   # keep yt-dlp current
        GLib.timeout_add(250, self._player_tick)
        self.connect("close-request", self._on_close)
        if self.category == "docs":
            self._set_category("docs")
        else:
            self.run_search("")

    def _on_view_changed(self, views, _p):
        name = views.get_visible_child_name()
        if name == "cameras":
            self.cameras.on_shown()
        else:
            self.cameras.on_hidden()
        if name == "live":
            self.livetv.on_shown()
        elif name == "youtube":
            self.youtube.on_shown()

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
        open_menu.append("Play a YouTube link…", "win.open-youtube")
        ob = Gtk.MenuButton(icon_name="list-add-symbolic", menu_model=open_menu,
                            tooltip_text="Open link, torrent or video")
        hb.pack_start(ob)

        menu = Gio.Menu()
        menu.append("English movies only", "win.english-only")
        menu.append("Auto English subtitles", "win.auto-english")
        menu.append("Show unverified titles", "win.show-unverified")
        menu.append("Watch together…", "win.together")
        menu.append("Phone remote", "win.phone-remote")
        menu.append("Video technology", "win.video-tech")
        menu.append("Open downloads folder", "win.open-downloads")
        menu.append("Check for updates", "win.check-updates")
        menu.append("Help & diagnostics", "win.diagnostics")
        menu.append(f"About {APP_NAME}", "win.about")
        hb.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, primary=True))
        tv.add_top_bar(hb)

        for name, cb in (("open-magnet", self.ask_magnet), ("open-torrent", self.pick_torrent),
                         ("open-video", self.pick_video), ("about", self.show_about),
                         ("open-youtube", self.ask_youtube),
                         ("diagnostics", self.show_diagnostics),
                         ("phone-remote", lambda *_: self.show_remote()),
                         ("together", lambda *_: self.together.show()),
                         ("video-tech", lambda *_: self.show_tech()),
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
        self.search.connect("activate", lambda e: self.play_youtube(e.get_text()) if youtube.is_link(e.get_text())
                            else self.run_search(e.get_text()))
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
        sug = Gtk.Button(label="✨ Suggest films", css_classes=["pill", "suggested-action"],
                         tooltip_text="Describe what you feel like watching in the search box, e.g. "
                                      "“a quiet, heartfelt film about a father and son”, then press this. "
                                      "AI on this computer finds real films that match.")
        sug.connect("clicked", lambda *_: self.suggest_films(self.search.get_text()))
        cats.append(sug)
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
        inner.append(self._build_recent_shelf())
        inner.append(self._build_ai_shelf())
        inner.append(self._build_web_shelf())
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
        # Cameras – the user's own security cameras, DVRs and NVRs
        self.cameras = CamerasPage(self)
        self.views.add_titled_with_icon(self.cameras, "cameras", "Cameras", "camera-web-symbolic")
        # YouTube – free full movies posted by official studio channels
        self.youtube = YouTubePage(self)
        self.views.add_titled_with_icon(self.youtube, "youtube", "YouTube", "media-playback-start-symbolic")
        self.views.connect("notify::visible-child-name", self._on_view_changed)

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
        for prop in ("audio_delay", "sub_scale"):           # remembered sync / size preferences
            if self.settings.get(prop) is not None:
                try:
                    setattr(self.video.mpv, prop, self.settings[prop])
                except Exception:
                    pass

        # top bar
        top = Gtk.Box(spacing=8, css_classes=["osd", "controls"], valign=Gtk.Align.START)
        back = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text="Back (Esc)", css_classes=["flat", "circular"])
        back.connect("clicked", lambda *_: self.leave_player())
        top.append(back)
        self.p_title = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END, css_classes=["heading"])
        top.append(self.p_title)
        self.p_trust = Gtk.Label(css_classes=["badge", "success"])
        top.append(self.p_trust)
        self.p_hdr = Gtk.Label(css_classes=["badge", "hdr-badge"], visible=False,
                               tooltip_text="High dynamic range – shown with tone mapping on this screen")
        top.append(self.p_hdr)
        self.sleep_btn = Gtk.MenuButton(icon_name="weather-clear-night-symbolic", tooltip_text="Sleep timer",
                                        css_classes=["flat"])
        self.sleep_btn.set_create_popup_func(self._build_sleep_popover)
        top.append(self.sleep_btn)
        top.append(self.together.button)
        pip = Gtk.Button(icon_name="view-restore-symbolic", css_classes=["flat", "circular"],
                         tooltip_text="Picture-in-picture: keep watching in a corner while you browse (I)")
        pip.connect("clicked", lambda *_: self.mini_player("corner"))
        top.append(pip)
        rb_ = Gtk.Button(icon_name="phone-symbolic", tooltip_text="Use your phone as a remote",
                         css_classes=["flat", "circular"])
        rb_.connect("clicked", lambda *_: self.show_remote())
        top.append(rb_)
        self.cast_btn = Gtk.MenuButton(icon_name="video-display-symbolic", tooltip_text="Play on a TV",
                                       css_classes=["flat", "circular"])
        self.cast_btn.set_create_popup_func(self._build_cast_popover)
        top.append(self.cast_btn)
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
        self._build_seek_preview()
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
        # inch backwards / forwards
        for label, secs, tip in (("‹ 1s", -1, "Back 1 second (Shift+←) · Ctrl+← = 5 s · “,” = one frame when paused"),
                                 ("1s ›", 1, "Forward 1 second (Shift+→) · Ctrl+→ = 5 s · “.” = one frame when paused")):
            b = Gtk.Button(label=label, tooltip_text=tip, css_classes=["flat", "nudge"])
            b.connect("clicked", lambda _b, s=secs: self.nudge(s))
            row.append(b)
        self.time_lbl = Gtk.Label(label="0:00 / --:--", css_classes=["numeric", "caption"], margin_start=6)
        row.append(self.time_lbl)
        self.dl_lbl = Gtk.Label(hexpand=True, xalign=1, css_classes=["dim-label", "caption", "numeric"], margin_end=6)
        row.append(self.dl_lbl)
        self.vol = Gtk.ScaleButton.new(0, 150, 5, ["audio-volume-muted-symbolic", "audio-volume-high-symbolic",
                                                    "audio-volume-low-symbolic", "audio-volume-medium-symbolic"])
        self.vol.set_value(self.settings.get("volume", 100))
        self.vol.connect("value-changed", self._on_volume)
        for label, step, tip in (("−", -1, "Volume down 1% (Shift+↓)"), ("+", 1, "Volume up 1% (Shift+↑)")):
            b = Gtk.Button(label=label, tooltip_text=tip, css_classes=["flat", "circular", "nudge"])
            b.connect("clicked", lambda _b, st=step: self.nudge_volume(st))
            row.append(b)
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

        # picture-in-picture controls (shown on hover while small)
        mb_ = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["mini-bar"])
        self.mini_title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["caption-heading"],
                                    margin_start=10, margin_end=10, margin_top=8)
        mb_.append(self.mini_title)
        mid = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, vexpand=True)
        b = Gtk.Button(icon_name="media-seek-backward-symbolic", tooltip_text="Back 10 s",
                       css_classes=["circular", "osd"])
        b.connect("clicked", lambda *_: self.seek_rel(-10))
        mid.append(b)
        self.mini_pp = Gtk.Button(icon_name="media-playback-pause-symbolic", tooltip_text="Play/Pause",
                                  css_classes=["circular", "osd", "mini-pp"])
        self.mini_pp.connect("clicked", lambda *_: self.toggle_pause())
        mid.append(self.mini_pp)
        b = Gtk.Button(icon_name="media-seek-forward-symbolic", tooltip_text="Forward 30 s",
                       css_classes=["circular", "osd"])
        b.connect("clicked", lambda *_: self.seek_rel(30))
        mid.append(b)
        mb_.append(mid)
        bot = Gtk.Box(spacing=4, margin_start=6, margin_end=6, margin_bottom=6)
        bot.append(Gtk.Box(hexpand=True))
        self.mini_swap = Gtk.Button(css_classes=["circular", "osd"])
        self.mini_swap.connect("clicked", lambda *_: self.mini_player("window" if self.mini == "corner" else "corner"))
        bot.append(self.mini_swap)
        b = Gtk.Button(icon_name="view-fullscreen-symbolic", tooltip_text="Back to the full player (double-click)",
                       css_classes=["circular", "osd"])
        b.connect("clicked", lambda *_: self.expand_player())
        bot.append(b)
        b = Gtk.Button(icon_name="window-close-symbolic", tooltip_text="Stop watching", css_classes=["circular", "osd"])
        b.connect("clicked", lambda *_: self.leave_player())
        bot.append(b)
        mb_.append(bot)
        self.mini_rev = Gtk.Revealer(child=mb_, transition_type=Gtk.RevealerTransitionType.CROSSFADE,
                                     reveal_child=False, visible=False)
        ov.add_overlay(self.mini_rev)
        self.mini_prog = Gtk.ProgressBar(valign=Gtk.Align.END, css_classes=["mini-prog"], visible=False)
        ov.add_overlay(self.mini_prog)

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

        # "Playing on <TV>" banner while casting
        cb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, css_classes=["osd", "buffer-box"],
                     halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, visible=False)
        cb.append(Gtk.Image(icon_name="video-display-symbolic", pixel_size=64))
        self.cast_lbl = Gtk.Label(css_classes=["title-2"])
        cb.append(self.cast_lbl)
        self.cast_sub = Gtk.Label(css_classes=["dim-label"], wrap=True, max_width_chars=48,
                                  justify=Gtk.Justification.CENTER)
        cb.append(self.cast_sub)
        stopc = Gtk.Button(label="Stop casting – watch here", css_classes=["pill"], halign=Gtk.Align.CENTER)
        stopc.connect("clicked", lambda *_: self.stop_casting(resume_here=True))
        cb.append(stopc)
        self.cast_box = cb
        ov.add_overlay(cb)
        self.cast_dev = None
        self._tv_stream = None
        self._cast_offset = 0
        self._cast_playing = False
        self._cast_status = {}
        self._cast_poll = 0

        # Skip intro / credits, and Next episode
        sk = Gtk.Box(spacing=8, halign=Gtk.Align.END, valign=Gtk.Align.END, margin_end=24, margin_bottom=110)
        self.skip_btn = Gtk.Button(css_classes=["osd", "pill", "skip-btn"], visible=False)
        self.skip_btn.connect("clicked", lambda *_: self._do_skip(manual=True))
        sk.append(self.skip_btn)
        self.next_btn = Gtk.Button(css_classes=["osd", "pill", "suggested-action", "skip-btn"], visible=False)
        self.next_btn.connect("clicked", lambda *_: self._play_next())
        sk.append(self.next_btn)
        ov.add_overlay(sk)
        self._skip = {"path": None, "segs": [], "done": set(), "next": None, "cd": None}

        # "Continue where you left off?" prompt
        rb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, css_classes=["osd", "buffer-box"],
                     halign=Gtk.Align.CENTER, valign=Gtk.Align.END, margin_bottom=120, visible=False)
        rb.append(Gtk.Label(label="Continue where you left off?", css_classes=["title-3"]))
        self.resume_info = Gtk.Label(css_classes=["dim-label"])
        rb.append(self.resume_info)
        btns = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER)
        self.resume_btn = Gtk.Button(css_classes=["suggested-action", "pill"])
        self.resume_btn.connect("clicked", lambda *_: self._resume_choice(True))
        restart = Gtk.Button(label="⟲  Start over", css_classes=["pill"])
        restart.connect("clicked", lambda *_: self._resume_choice(False))
        btns.append(self.resume_btn)
        btns.append(restart)
        rb.append(btns)
        self.resume_cd_lbl = Gtk.Label(css_classes=["dim-label", "caption"])
        rb.append(self.resume_cd_lbl)
        self.resume_box = rb
        ov.add_overlay(rb)

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        ov.add_controller(motion)
        click = Gtk.GestureClick()
        click.connect("pressed", self._on_click)
        self.video.add_controller(click)
        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.BOTH_AXES)
        scroll.connect("scroll", self._on_video_scroll)
        self.video.add_controller(scroll)
        self._last_motion = (0, 0)
        return ov

    # ── "describe what you feel like watching" (local AI) ──────────────────
    def _build_ai_shelf(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_start=16, margin_end=16,
                      margin_bottom=10, visible=False)
        head = Gtk.Box(spacing=8)
        self.ai_lbl = Gtk.Label(xalign=0, hexpand=True, wrap=True, css_classes=["title-4"], margin_start=6)
        head.append(self.ai_lbl)
        close = Gtk.Button(icon_name="window-close-symbolic", css_classes=["flat", "circular"],
                           tooltip_text="Hide suggestions")
        close.connect("clicked", lambda *_: (setattr(self, "ai_gen", self.ai_gen + 1), box.set_visible(False)))
        head.append(close)
        box.append(head)
        self.ai_sub = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label", "caption"], margin_start=6)
        box.append(self.ai_sub)
        self.ai_row = Gtk.Box(spacing=6)
        hs = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True)
        hs.set_child(self.ai_row)
        self.ai_scroll = hs
        box.append(hs)
        self.ai_box = box
        self.ai_gen = 0
        return box

    def suggest_films(self, text):
        text = text.strip()
        if len(text.split()) < 2:
            self.toasts.add_toast(Adw.Toast(title="Describe what you feel like watching in the search box first – "
                                                  "e.g. “funny space adventure for the family”", timeout=5))
            self.search.grab_focus()
            return
        self.ai_gen += 1
        gen = self.ai_gen
        while (c := self.ai_row.get_first_child()):
            self.ai_row.remove(c)
        self.ai_scroll.set_visible(False)
        self.ai_box.set_visible(True)
        self.ai_lbl.set_label(f"✨ Suggestions for “{text}”")
        self.ai_sub.set_label("Starting the AI on this computer…")
        if self.disc_stack.get_visible_child_name() != "results":
            self.disc_stack.set_visible_child_name("results")
        countries = self.mylist.countries() if hasattr(self, "mylist") else ["US"]

        def say(msg):
            GLib.idle_add(lambda: gen == self.ai_gen and self.ai_sub.set_label(msg) and False)

        def work():
            try:
                res, used = recommend.suggest(text, countries, progress=say)
                GLib.idle_add(done, res, used, None)
            except recommend.NoModel as e:
                GLib.idle_add(done, None, None, str(e))
            except Exception as e:
                msg = ("The film catalogue is busy – try again in a minute" if "429" in str(e)
                       else "Couldn't get suggestions – check your connection")
                GLib.idle_add(done, None, None, msg)

        def done(res, used, err):
            if gen != self.ai_gen:
                return False
            if err:
                self.ai_sub.set_label(err)
                return False
            if not res:
                self.ai_sub.set_label("No films matched – try describing it differently")
                return False
            for t in res:
                card = StreamCard(self.mylist, t)
                card.set_tooltip_text(t.get("why") or None)
                self.ai_row.append(card)
            self.ai_scroll.set_visible(True)
            self.ai_sub.set_label(f"Real films picked and ranked by AI on this computer ({used}). "
                                  "Hover a film to see what it's about. Free options are listed first.")
            return False
        threading.Thread(target=work, daemon=True).start()

    # ── search everywhere (YouTube official uploads + every streaming service) ──
    def _build_web_shelf(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_start=16, margin_end=16,
                      margin_bottom=10, visible=False)
        self.web_lbl = Gtk.Label(xalign=0, css_classes=["title-4"], margin_start=6)
        box.append(self.web_lbl)
        self.web_yt_lbl = Gtk.Label(label="On YouTube – free, posted by the studio", xalign=0,
                                    css_classes=["heading"], margin_start=6, margin_top=4)
        box.append(self.web_yt_lbl)
        self.web_yt_row = Gtk.Box(spacing=6)
        hs = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True)
        hs.set_child(self.web_yt_row)
        self.web_yt_scroll = hs
        box.append(hs)
        self.web_st_lbl = Gtk.Label(label="Streaming services – free ones first", xalign=0,
                                    css_classes=["heading"], margin_start=6, margin_top=4)
        box.append(self.web_st_lbl)
        self.web_st_row = Gtk.Box(spacing=6)
        hs2 = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True)
        hs2.set_child(self.web_st_row)
        self.web_st_scroll = hs2
        box.append(hs2)
        self.web_box = box
        self._web_found = 0
        return box

    def _search_web(self, gen, text):
        for row in (self.web_yt_row, self.web_st_row):
            while (c := row.get_first_child()):
                row.remove(c)
        self._web_found = 0
        if not text.strip():
            self.web_box.set_visible(False)
            return
        self.web_box.set_visible(True)
        for w in (self.web_yt_lbl, self.web_yt_scroll, self.web_st_lbl, self.web_st_scroll):
            w.set_visible(False)
        self.web_lbl.set_label("Searching YouTube and every streaming service…")
        countries = self.mylist.countries() if hasattr(self, "mylist") else ["US"]

        def work():
            res = websearch.everywhere(text, countries)
            GLib.idle_add(done, res)

        def done(res):
            if gen != self.search_gen:
                return False
            yt, st = res["youtube"], res["streaming"]
            for f in yt:
                card = FilmCard(self, f)
                click = Gtk.GestureClick()
                click.connect("released", lambda *_a, f=f: self.youtube.show_details(f))
                card.add_controller(click)
                self.web_yt_row.append(card)
            for t in st:
                self.web_st_row.append(StreamCard(self.mylist, t))
            self.web_yt_lbl.set_visible(bool(yt))
            self.web_yt_scroll.set_visible(bool(yt))
            self.web_st_lbl.set_visible(bool(st))
            self.web_st_scroll.set_visible(bool(st))
            self._web_found = len(yt) + len(st)
            if self._web_found:
                self.web_lbl.set_label(f"Found across the web for “{text}”")
                if self.disc_stack.get_visible_child_name() == "empty":
                    self.disc_stack.set_visible_child_name("results")
                    self.free_lbl.set_visible(False)
            else:
                self.web_lbl.set_label("Not found on YouTube or any streaming service"
                                       + (" (offline?)" if res["errors"] else ""))
            return False
        threading.Thread(target=work, daemon=True).start()

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
        from .watch import COUNTRIES
        names = " & ".join(COUNTRIES[c] for c in countries)
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
        if hasattr(self, "web_box"):
            self._search_web(gen, text)

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
        has_web = (bool(text.strip()) and getattr(self, "_web_found", 0)) or self.ai_box.get_visible()
        if text.strip():
            self.free_lbl.set_label("Free to watch right here – plays in CineHaven")
            self.free_lbl.set_visible(bool(shown))
        self.disc_stack.set_visible_child_name("results" if shown or has_web else "empty")

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
                        "year": r.get("year"), "kind": "documentary" if self.category == "docs" else "movie",
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
        self._stop_ai_subs()
        self.pending_play = None
        self._remember_position()
        self.resume_key = f"item:{it.key}"
        start = self._saved_pos(self.resume_key)
        if it.is_complete():
            path = it.file_info()[0]
            url = path
            self.local_path = path
            start = start or self._saved_pos(path)
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
        ia = it.meta.get("ia_id")
        self._add_history("item", it.key, it.meta.get("title") or it.title, catalog.THUMB_URL + ia if ia else None)
        self._offer_resume(start)
        self._waiting_first_frame = time.time()

    # ── player control ───────────────────────────────────────────────────
    def enter_player(self, title, item):
        self.current = item
        self.together_media = None
        self.p_title.set_label(title)
        trust = (item.meta.get("trust") if item else None)
        lbl = {"verified": "✔ Verified", "likely": "✔ Free to share"}.get(trust)
        self.p_trust.set_visible(bool(lbl))
        self.p_trust.set_label(lbl or "")
        self.seek.set_value(0)
        self.seek.set_fill_level(0)
        self.time_lbl.set_label("0:00 / --:--")
        self.dl_lbl.set_label("")
        self._set_mode("player", self.mini if self.mini == "window" else None)
        self.video.mpv.volume = self.vol.get_value()
        self._show_controls()

    def leave_player(self):
        if self.cast_dev:
            self.stop_casting(resume_here=False)
        self._stop_ai_subs()
        self.doctor.stop()
        self._remember_position()
        self._hide_resume()
        self.resume_key = None
        self.local_path = None
        self.pending_play = None
        self.together_media = None
        self._stop_translation()
        self.video.stop()
        if self.is_fullscreen():
            self.unfullscreen()
        self.root.set_visible_child_name("browse")
        self.set_cursor(None)
        if self.views.get_visible_child_name() == "cameras":
            self.cameras.on_shown()
        self._refresh_recent()
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
        if self.cast_dev:
            playing = self._cast_status.get("state") in ("PLAYING", "BUFFERING")
            self._cast_do(self.cast_dev.pause if playing else self.cast_dev.resume)
            self._cast_status["state"] = "PAUSED" if playing else "PLAYING"
            self._show_controls()
            return
        self.video.mpv.pause = not self.video.mpv.pause
        self._show_controls()

    def seek_rel(self, s):
        if self.cast_dev:
            pos = self._cast_status.get("pos") or 0
            self._cast_status["pos"] = max(0, pos + s)
            self._cast_do(lambda: self._cast_seek(max(0, pos + s)))
            self._show_controls()
            return
        try:
            self.video.mpv.seek(s, "relative")
        except Exception:
            pass
        self._show_controls()

    # ── inching: small, exact steps ──────────────────────────────────────
    def _osd(self, text):
        try:
            self.video.mpv.show_text(text, 900)
        except Exception:
            pass

    def nudge(self, secs):
        """Move a little backwards or forwards – exactly, not to the nearest keyframe."""
        if self.cast_dev:
            self.seek_rel(secs)
            return
        m = self.video.mpv
        try:
            m.seek(secs, "relative", "exact")
            pos = max(0, (m.time_pos or 0) + secs)
            self._osd(f"{'⏪' if secs < 0 else '⏩'} {abs(secs):g} s  ·  {fmt_time(pos)}")
        except Exception:
            pass
        self._show_controls()

    def frame_step(self, direction):
        """One frame at a time (pauses the film)."""
        if self.cast_dev:
            return
        m = self.video.mpv
        try:
            m.command("frame-step" if direction > 0 else "frame-back-step")
            self._osd(f"{'◀' if direction < 0 else '▶'} 1 frame  ·  {fmt_time(m.time_pos or 0)}")
        except Exception:
            pass
        self._show_controls()

    def nudge_volume(self, step):
        v = max(0, min(150, round(self.vol.get_value()) + step))
        self.vol.set_value(v)
        self._osd(f"🔊 Volume {v:g}%")
        self._show_controls()

    def _on_video_scroll(self, _c, dx, dy):
        if dy:
            self.nudge_volume(-2 if dy > 0 else 2)
        elif dx:
            self.nudge(1 if dx > 0 else -1)
        return True

    def _on_seek(self, _scale, _scroll, value):
        if self.cast_dev:
            self._cast_status["pos"] = max(0, value)
            self._cast_do(lambda: self._cast_seek(max(0, value)))
            return False
        try:
            self.video.mpv.seek(max(0, value), "absolute", "default-precise")
        except Exception:
            pass
        return False

    def _on_volume(self, _b, v):
        if self.cast_dev:
            self._cast_do(lambda: self.cast_dev.volume(min(100, v)))
            return
        self.video.mpv.volume = v
        self.settings["volume"] = v

    # ── picture-in-picture ───────────────────────────────────────────────
    def _set_mode(self, name, mini=None):
        self.root.name = name
        was = self.mini
        self.mini = mini if name == "player" else None
        p, b = self._player_w, self._browse_w
        corner, small = self.mini == "corner", self.mini == "window"
        p.set_visible(name == "player")
        b.set_visible(name != "player" or corner)
        if corner:
            w = int(max(320, min(520, self.get_width() * 0.3)))
            p.set_size_request(w, w * 9 // 16)
            p.set_halign(Gtk.Align.END)
            p.set_valign(Gtk.Align.END)
            for side in ("top", "bottom", "start", "end"):
                getattr(p, f"set_margin_{side}")(18)
            p.add_css_class("mini-player")
            p.set_overflow(Gtk.Overflow.HIDDEN)
        else:
            p.set_size_request(-1, -1)
            p.set_halign(Gtk.Align.FILL)
            p.set_valign(Gtk.Align.FILL)
            for side in ("top", "bottom", "start", "end"):
                getattr(p, f"set_margin_{side}")(0)
            p.remove_css_class("mini-player")
        self.top_rev.set_visible(not self.mini)
        self.bot_rev.set_visible(not self.mini)
        self.mini_rev.set_visible(bool(self.mini))
        self.mini_rev.set_reveal_child(False)
        self.mini_prog.set_visible(bool(self.mini))
        if self.mini:
            self.mini_title.set_label(self.p_title.get_label())
            self.mini_swap.set_icon_name("view-dual-symbolic" if small else "window-new-symbolic")
            self.mini_swap.set_tooltip_text("Back into the CineHaven window's corner" if small else
                                            "Pop out as a small window you can keep beside other apps")
        if small and was != "window":
            self._full_size = (self.get_width(), self.get_height(), self.is_maximized())
            if self.is_fullscreen():
                self.unfullscreen()
            self.unmaximize()
            self.set_default_size(480, 270)
            self.set_title(self.p_title.get_label() or APP_NAME)
            if not self.settings.get("pip_tip_shown"):
                self.settings["pip_tip_shown"] = True
                save_settings(self.settings)
                self.toasts.add_toast(Adw.Toast(timeout=8, title="Tip: to keep it above other windows, "
                                                "press Alt+Space and choose “Always on Top”"))
        elif was == "window" and not small and self._full_size:
            w, h, maxed = self._full_size
            self._full_size = None
            self.set_default_size(w, h)
            if maxed:
                self.maximize()
            self.set_title(APP_NAME)

    def mini_player(self, where="corner"):
        if self.root.get_visible_child_name() != "player":
            return
        if self.is_fullscreen():
            self.unfullscreen()
        self._set_mode("player", where)
        self.set_cursor(None)
        if where == "corner":
            self.set_focus(None)

    def expand_player(self):
        if self.root.get_visible_child_name() == "player":
            self._set_mode("player", None)
            self._show_controls()

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
                if self._ai_media():
                    opts.append((("ai", "translate"), "✨ AI subtitles – English (any language)"))
                    opts.append((("ai", "transcribe"), "✨ AI subtitles – language spoken"))
                    if getattr(self, "_ai", None):
                        cur = ("ai", self._ai.task)
            for val, name in opts:
                active = cur == val or (val is False and not cur)
                cb = Gtk.CheckButton(label=name, group=group, active=active)
                group = group or cb
                cb.connect("toggled", lambda b, p=prop, v=val: b.get_active() and self._select_track(p, v))
                box.append(cb)
        add = Gtk.Button(label="Load subtitle file…", css_classes=["flat"], margin_top=6)
        add.connect("clicked", lambda *_: (pop.popdown(), self.pick_subtitle()))
        box.append(add)
        box.append(Gtk.Separator(margin_top=6, margin_bottom=2))
        box.append(Gtk.Label(label="Picture & sound", xalign=0, css_classes=["heading"], margin_top=4))
        for key, title, sub in (
                ("enhance_picture", "Enhance picture", "AI upscaling, sharper detail, less noise"),
                ("dialogue_boost", "Dialogue boost", "Clearer voices over music and effects"),
                ("night_mode", "Night mode", "No sudden loud bangs; quiet parts stay audible"),
                ("auto_skip", "Skip intros automatically", "Also recaps; found from chapters or the theme tune"),
                ("autoplay_next", "Play next episode", "Starts the next episode when the credits roll")):
            row = Gtk.Box(spacing=12, margin_top=2)
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
            col.append(Gtk.Label(label=title, xalign=0))
            note = Gtk.Label(label=sub, xalign=0, css_classes=["dim-label", "caption"])
            col.append(note)
            if key == "enhance_picture" and self.settings.get(key):
                live = enhance.describe(self.video.mpv)
                if live:
                    note.set_label(f"On now: {live}")
            row.append(col)
            sw = Gtk.Switch(active=self._setting(key), valign=Gtk.Align.CENTER)
            sw.connect("notify::active", lambda w, _p, k=key: self._set_enhancement(k, w.get_active()))
            row.append(sw)
            box.append(row)
        box.append(Gtk.Separator(margin_top=6, margin_bottom=2))
        box.append(Gtk.Label(label="Timing & speed", xalign=0, css_classes=["heading"], margin_top=4))
        box.append(self._stepper("Subtitle timing", "sub_delay", 0.1, -60, 60,
                                 lambda v: f"{v:+.1f} s" if abs(v) > 0.04 else "in sync",
                                 "Subtitles too early? Press +. Too late? Press −  (keys: Z / X)"))
        box.append(self._stepper("Sound delay", "audio_delay", 0.05, -5, 5,
                                 lambda v: f"{v:+.2f} s" if abs(v) > 0.01 else "in sync",
                                 "For Bluetooth headphones and soundbars: lips and voice out of step? "
                                 "Remembered for next time.", remember="audio_delay"))
        box.append(self._stepper("Subtitle size", "sub_scale", 0.1, 0.5, 2.5,
                                 lambda v: f"{round(v * 100)}%", "Remembered for next time.",
                                 remember="sub_scale"))
        sp = Gtk.Box(spacing=4, margin_top=4)
        sp.append(Gtk.Label(label="Speed", xalign=0, hexpand=True))
        grp = None
        cur = round(self.video.mpv.speed or 1, 2)
        for v in (0.75, 1.0, 1.25, 1.5, 2.0):
            b = Gtk.ToggleButton(label=f"{v:g}×", group=grp, active=abs(cur - v) < 0.01, css_classes=["flat"])
            grp = grp or b
            b.connect("toggled", lambda btn, v=v: btn.get_active() and setattr(self.video.mpv, "speed", v))
            sp.append(b)
        sp.set_tooltip_text("Voices keep their normal pitch at any speed  (keys: [ and ], Backspace = normal)")
        box.append(sp)
        pop.set_child(box)
        mb.set_popover(pop)

    def _stepper(self, title, prop, step, lo, hi, fmt, tip, remember=None):
        """A row with − value + for an mpv property."""
        row = Gtk.Box(spacing=4, tooltip_text=tip)
        row.append(Gtk.Label(label=title, xalign=0, hexpand=True))
        m = self.video.mpv
        val = Gtk.Label(width_chars=8, css_classes=["numeric", "dim-label"])

        def show():
            try:
                val.set_label(fmt(float(getattr(m, prop) or 0)))
            except Exception:
                val.set_label("–")

        def change(d):
            try:
                v = round(min(hi, max(lo, float(getattr(m, prop) or 0) + d)), 2)
                setattr(m, prop, v)
                if remember:
                    self.settings[remember] = v
                    save_settings(self.settings)
            except Exception:
                pass
            show()
        minus = Gtk.Button(label="−", css_classes=["flat", "circular", "nudge"])
        minus.connect("clicked", lambda *_: change(-step))
        plus = Gtk.Button(label="+", css_classes=["flat", "circular", "nudge"])
        plus.connect("clicked", lambda *_: change(step))
        for w in (minus, val, plus):
            row.append(w)
        show()
        return row

    # ── HDR ─────────────────────────────────────────────────────────────
    def _update_hdr_badge(self, m):
        try:
            vp = m.video_params or {}
        except Exception:
            vp = {}
        gamma, matrix = vp.get("gamma"), vp.get("colormatrix") or vp.get("matrix")
        name = ("Dolby Vision" if matrix == "dolbyvision" else "HDR10" if gamma == "pq"
                else "HLG HDR" if gamma == "hlg" else None)
        self.p_hdr.set_label(name or "")
        self.p_hdr.set_visible(bool(name))

    # ── seek-bar preview pictures ────────────────────────────────────────
    def _build_seek_preview(self):
        pop = Gtk.Popover(autohide=False, has_arrow=True, position=Gtk.PositionType.TOP, can_focus=False,
                          css_classes=["seek-preview"])
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.prev_pic = _Thumb(240, 135)
        box.append(self.prev_pic)
        self.prev_lbl = Gtk.Label(css_classes=["numeric", "heading"])
        box.append(self.prev_lbl)
        pop.set_child(box)
        pop.set_parent(self.seek)
        self.prev_pop = pop
        self._prev = {"src": None, "cache": {}, "want": None, "busy": False, "timer": None}
        mo = Gtk.EventControllerMotion()
        mo.connect("motion", self._seek_hover)
        mo.connect("leave", lambda *_: pop.popdown())
        self.seek.add_controller(mo)

    def _preview_source(self):
        """A file or local stream address ffmpeg can grab frames from quickly (not live/YouTube)."""
        if self.live or self.cast_dev:
            return None
        if self.local_path and os.path.isfile(self.local_path):
            return self.local_path
        try:
            p = self.video.mpv.path or ""
        except Exception:
            return None
        return p if p.startswith("http://127.0.0.1") and "/" in p[17:] and not p.startswith("edl") else None

    def _seek_hover(self, _c, x, _y):
        try:
            dur = self.video.mpv.duration
        except Exception:
            dur = None
        if not dur or self.live:
            self.prev_pop.popdown()
            return
        w = self.seek.get_width()
        pad = 10                                            # the slider's own margins
        t = max(0.0, min(dur, (x - pad) / max(1, w - 2 * pad) * dur))
        self.prev_lbl.set_label(fmt_time(t))
        r = Gdk.Rectangle()
        r.x, r.y, r.width, r.height = int(x), 0, 1, 1
        self.prev_pop.set_pointing_to(r)
        src = self._preview_source()
        self.prev_pic.set_visible(bool(src))
        if not self.prev_pop.get_visible():
            self.prev_pop.popup()
        if not src:
            return
        st = self._prev
        if st["src"] != src:
            st.update(src=src, cache={})
        key = int(t // 5) * 5                               # a picture every 5 seconds is plenty
        if key in st["cache"]:
            self.prev_pic.set_paintable(st["cache"][key])
            return
        st["want"] = key
        if st["timer"]:
            GLib.source_remove(st["timer"])
        st["timer"] = GLib.timeout_add(120, self._grab_preview)

    def _grab_preview(self):
        st = self._prev
        st["timer"] = None
        if st["busy"] or st["want"] is None:
            return False
        key, src = st["want"], st["src"]
        st["busy"] = True
        exe = skip._ffmpeg()

        def work():
            data = None
            if exe:
                try:
                    r = subprocess.run([exe, "-v", "error", "-ss", str(key + 2), "-i", src, "-frames:v", "1",
                                        "-vf", "scale=480:-2", "-c:v", "mjpeg", "-q:v", "5", "-f", "image2", "-"],
                                       capture_output=True, timeout=8)
                    data = r.stdout if r.returncode == 0 and r.stdout else None
                except Exception:
                    data = None
            GLib.idle_add(done, data)

        def done(data):
            st["busy"] = False
            if data and st["src"] == src:
                try:
                    tex = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
                    st["cache"][key] = tex
                    if st["want"] == key:
                        self.prev_pic.set_paintable(tex)
                except GLib.Error:
                    pass
            if st["want"] != key and st["want"] is not None:
                self._grab_preview()                        # the pointer moved on meanwhile
            return False
        threading.Thread(target=work, daemon=True).start()
        return False

    # ── sleep timer ──────────────────────────────────────────────────────
    def _build_sleep_popover(self, mb):
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6, margin_bottom=6,
                      margin_start=6, margin_end=6)
        box.append(Gtk.Label(label="Sleep timer", css_classes=["heading"], xalign=0, margin_bottom=4))
        if self._sleep:
            kind, at = self._sleep
            left = "at the end of this film" if kind == "end" else f"in {max(1, round((at - time.time()) / 60))} min"
            box.append(Gtk.Label(label=f"Pausing {left}", xalign=0, css_classes=["dim-label", "caption"]))
        for label, mins in (("15 minutes", 15), ("30 minutes", 30), ("45 minutes", 45), ("1 hour", 60),
                            ("1½ hours", 90), ("End of this film", "end"), ("Off", None)):
            b = Gtk.Button(label=label, css_classes=["flat"])
            b.get_child().set_xalign(0)
            b.connect("clicked", lambda _b, v=mins: (pop.popdown(), self.set_sleep(v)))
            box.append(b)
        pop.set_child(box)
        mb.set_popover(pop)

    def set_sleep(self, mins):
        if mins is None:
            self._sleep = None
            self.toast("Sleep timer off")
        elif mins == "end":
            self._sleep = ("end", None)
            self.toast("CineHaven will stop at the end of this film")
        else:
            self._sleep = ("at", time.time() + mins * 60)
            self.toast(f"CineHaven will fade out and pause in {mins} minutes")
        self._sleep_vol = None
        self.sleep_btn.set_icon_name("alarm-symbolic" if self._sleep else "weather-clear-night-symbolic")

    def _sleep_tick(self, m, pos, dur, paused):
        if not self._sleep or paused:
            return
        kind, at = self._sleep
        if kind == "end":
            if dur and pos is not None and dur - pos < 30:
                at = time.time() + (dur - pos)
            else:
                return
        left = at - time.time()
        if left <= 0:
            m.pause = True
            if getattr(self, "_sleep_vol", None) is not None:
                m.volume = self._sleep_vol
            self._sleep = None
            self._sleep_vol = None
            self.sleep_btn.set_icon_name("weather-clear-night-symbolic")
            self.toast("Sleep timer: paused. Sleep well 🌙")
        elif left < 30:                                     # fade out over the last 30 seconds
            if getattr(self, "_sleep_vol", None) is None:
                self._sleep_vol = m.volume
            m.volume = self._sleep_vol * max(0.0, left / 30)

    # ── desktop media controls (MPRIS) ───────────────────────────────────
    def mpris_state(self):
        if self.root.get_visible_child_name() != "player":
            return {"status": "Stopped"}
        m = self.video.mpv
        try:
            paused, pos, dur = m.pause, m.time_pos, m.duration
        except Exception:
            paused, pos, dur = True, None, None
        h = self.settings.get("history", [])
        art = h[0].get("thumb") if h and h[0].get("key") == self.resume_key else None
        return {"status": "Paused" if paused else "Playing", "title": self.p_title.get_label(),
                "length": None if self.live else dur, "position": pos, "art": art, "rate": m.speed or 1.0,
                "volume": self.vol.get_value(), "can_next": bool(self._skip.get("next")), "live": self.live}

    def mpris_command(self, c, v):
        GLib.idle_add(self._mpris_do, c, v)

    def _mpris_do(self, c, v):
        in_player = self.root.get_visible_child_name() == "player"
        m = self.video.mpv
        try:
            if c == "raise":
                self.present()
            elif c == "quit":
                self.close()
            elif c == "open" and v:
                self.present()
                self.open_any(v[7:] if v.startswith("file://") else v)
            elif not in_player:
                if c in ("play", "toggle"):
                    h = self.settings.get("history", [])
                    if h:
                        self._open_recent(h[0])             # play key with nothing on: carry on the last film
            elif c == "toggle":
                self.toggle_pause()
            elif c == "play":
                m.pause = False
            elif c == "pause":
                m.pause = True
            elif c == "stop":
                self.leave_player()
            elif c == "next":
                if self._skip.get("next"):
                    self._play_next()
                else:
                    self.seek_rel(30)
            elif c == "previous":
                self.seek_rel(-10)
            elif c == "seek_rel":
                self.seek_rel(v)
            elif c == "seek_abs":
                self._on_seek(None, None, float(v))
            elif c == "volume":
                self.vol.set_value(max(0, min(150, v)))
            elif c == "rate":
                m.speed = max(0.25, min(4, v))
        except Exception as e:
            health.log.info("Media key %s failed: %s", c, e)
        return False

    def _mpris_tick(self):
        try:
            self.mpris.update(**self.mpris_state())
        except Exception:
            pass
        return True

    ON_BY_DEFAULT = ("auto_skip", "autoplay_next")

    def _setting(self, key):
        return bool(self.settings.get(key, key in self.ON_BY_DEFAULT))

    def _set_enhancement(self, key, on):
        self.settings[key] = on
        save_settings(self.settings)
        self._apply_enhancements()
        names = {"enhance_picture": "Enhance picture", "dialogue_boost": "Dialogue boost", "night_mode": "Night mode",
                 "auto_skip": "Skip intros automatically", "autoplay_next": "Play next episode"}
        self.toast(f"{names[key]} {'on' if on else 'off'}")

    def _apply_enhancements(self):
        m = self.video.mpv
        self._update_hdr_badge(m)
        out_h = self.video.get_height() * self.video.get_scale_factor()
        if getattr(self, "_pic_level", None) is None:
            self._pic_cls = adapt.video_class(m)
            self._pic_level = adapt.start_level(self._pic_cls) if self._pic_cls else 2
            self._smooth_s = 0
        try:
            enhance.apply_picture(m, bool(self.settings.get("enhance_picture")), out_h or None, self._pic_level)
            enhance.apply_audio(m, bool(self.settings.get("dialogue_boost")), bool(self.settings.get("night_mode")))
        except Exception as e:
            health.log.warning("Couldn't apply picture/sound settings: %s", e)

    def _reset_live(self):
        self.live = False
        self.seek.set_visible(True)
        m = self.video.mpv
        m.user_agent = "CineHaven"
        m.referrer = ""
        m.demuxer_lavf_analyzeduration = 0
        m.demuxer_lavf_o = ""
        m.cache_pause = True
        m.video_latency_hacks = False
        try:
            m["hls-bitrate"] = "max"
        except Exception:
            pass

    def play_live(self, ch, stream):
        self._remember_position()
        self._hide_resume()
        self.local_path = None
        self.resume_key = None
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
        if ch.get("camera"):                         # security camera: as close to real time as possible
            m.demuxer_lavf_o = "fflags=+nobuffer"
            m.demuxer_lavf_analyzeduration = 0.5
            m.cache_pause = False
            m.video_latency_hacks = True
            self.p_trust.set_label("● CAMERA")
            self.set_buffering("Connecting…", f"{ch['name']}", None)
        else:
            self.set_buffering("Tuning in…", f"{ch['name']} – live channels take about 10–20 seconds to start", None)
        self._lang_done = False
        idx = ch["streams"].index(stream) if stream in ch["streams"] else 0
        self.doctor.start("live", stream["url"], ch["name"], ch=ch, stream_idx=idx)
        self.video.play(stream["url"])

    # ── recently played ──────────────────────────────────────────────────
    HISTORY_MAX = 10

    def _add_history(self, kind, ref, title, thumb=None):
        self.together_media = self._shareable(kind, ref, title)
        key = self.resume_key
        if not key:
            return
        h = [e for e in self.settings.get("history", []) if e["key"] != key]
        old = next((e for e in self.settings.get("history", []) if e["key"] == key), {})
        rs = self.settings.get("resume", {}).get(key) or {}
        h.insert(0, {"key": key, "kind": kind, "ref": ref, "title": title, "thumb": thumb or old.get("thumb"),
                     "when": time.time(), "t": rs.get("t") or old.get("t"), "dur": rs.get("dur") or old.get("dur"),
                     "done": False if rs else old.get("done", False)})
        self.settings["history"] = h[:self.HISTORY_MAX]
        save_settings(self.settings)
        self._refresh_recent()

    def _shareable(self, kind, ref, title):
        """How a friend's CineHaven can open the same thing (for Watch together)."""
        if kind == "youtube":
            return {"kind": "youtube", "ref": ref, "title": title}
        if kind == "file":
            try:
                size = os.path.getsize(ref)
            except OSError:
                size = None
            return {"kind": "file", "ref": os.path.basename(ref), "size": size, "title": title}
        if kind == "item":
            it = self.engine.items.get(ref)
            ia = it.meta.get("ia_id") if it else None
            return {"kind": "ia", "ref": ia, "title": title} if ia else {"kind": "magnet", "ref": ref, "title": title}
        return None

    def _build_recent_shelf(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_start=16, margin_end=16,
                      margin_bottom=10, visible=False)
        box.append(Gtk.Label(label="Recently played", xalign=0, css_classes=["title-4"], margin_start=6))
        self.recent_row = Gtk.Box(spacing=10)
        hs = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True)
        hs.set_child(self.recent_row)
        box.append(hs)
        self.recent_box = box
        GLib.idle_add(lambda: (self._refresh_recent(), False)[1])
        return box

    def _refresh_recent(self):
        if not hasattr(self, "recent_row"):
            return
        row = self.recent_row
        while row.get_first_child():
            row.remove(row.get_first_child())
        hist = self.settings.get("history", [])[:self.HISTORY_MAX]
        self.recent_box.set_visible(bool(hist))
        for e in hist:
            row.append(self._recent_card(e))

    def _recent_card(self, e):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, css_classes=["recent-card"])
        card.set_size_request(208, -1)
        ov = Gtk.Overlay()
        pic = _Thumb(208, 117)
        ov.set_child(pic)
        icon = Gtk.Image(icon_name={"youtube": "media-playback-start-symbolic", "file": "folder-videos-symbolic"}
                         .get(e["kind"], "video-x-generic-symbolic"), pixel_size=36, css_classes=["dim-label"])
        ov.add_overlay(icon)
        t, dur = e.get("t") or 0, e.get("dur") or 0
        if dur and (t or e.get("done")):
            bar = Gtk.ProgressBar(fraction=1.0 if e.get("done") else min(1, t / dur), valign=Gtk.Align.END,
                                  css_classes=["recent-progress"])
            ov.add_overlay(bar)
        x = Gtk.Button(icon_name="window-close-symbolic", css_classes=["circular", "osd", "recent-x"],
                       halign=Gtk.Align.END, valign=Gtk.Align.START, margin_top=4, margin_end=4,
                       tooltip_text="Remove from Recently played")
        x.connect("clicked", lambda *_: self._forget_recent(e))
        ov.add_overlay(x)
        card.append(ov)
        card.append(Gtk.Label(label=e["title"], xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=26,
                              css_classes=["heading"]))
        if e.get("done"):
            sub = "Watched to the end · play again"
        elif t and dur:
            sub = f"Left off at {fmt_time(t)} of {fmt_time(dur)}"
        elif t:
            sub = f"Left off at {fmt_time(t)}"
        else:
            sub = "Just started"
        card.append(Gtk.Label(label=sub, xalign=0, css_classes=["dim-label", "caption"]))
        click = Gtk.GestureClick()
        click.connect("released", lambda *_: self._open_recent(e))
        card.add_controller(click)
        card.set_cursor_from_name("pointer")
        card.set_tooltip_text("Resume" if t and not e.get("done") else "Play")

        def show_thumb(tex):
            pic.set_paintable(tex)
            icon.set_visible(False)
        if e.get("thumb"):
            class _P:                                   # load_thumb wants something with set_paintable
                set_paintable = staticmethod(show_thumb)
            self.load_thumb(e["thumb"], _P)
        elif e["kind"] == "file" and os.path.isfile(e["ref"]):
            from . import localmedia

            def work():
                p = localmedia.thumbnail(e["ref"])
                if p:
                    GLib.idle_add(lambda: (show_thumb(Gdk.Texture.new_from_filename(p)), False)[1])
            self.thumbs.submit(work)
        return card

    def _forget_recent(self, e):
        self.settings["history"] = [h for h in self.settings.get("history", []) if h["key"] != e["key"]]
        save_settings(self.settings)
        self._refresh_recent()

    def _open_recent(self, e):
        kind, ref = e["kind"], e["ref"]
        if kind == "youtube":
            self.play_youtube(ref, e["title"])
        elif kind == "file":
            if os.path.isfile(ref):
                self.play_local(ref, e["title"])
            else:
                self.toast("That file has been moved or deleted")
        elif kind == "item":
            it = self.engine.items.get(ref)
            if it:
                self.watch_item(it)
            else:
                self.toast("It's no longer in your Library – find it again in Discover")
        return True

    # ── YouTube, inside CineHaven ─────────────────────────────────────────
    def play_youtube(self, link, title=None, start=None):
        vid = youtube.video_id(link)
        if not vid:
            self.toast("That isn't a YouTube video link")
            return
        if start is None:
            start = youtube.start_time(link)
        self._stop_ai_subs()
        self._remember_position()
        self._hide_resume()
        self.enter_player(title or "YouTube", None)
        self.p_trust.set_label("▶ YouTube")
        self.p_trust.set_visible(True)
        self.pending_play = None
        self._reset_live()
        self._stop_translation()
        self._lang_done = False
        self.local_path = None
        self.resume_key = None
        self.video.stop()
        self.set_buffering("Opening YouTube…", title or "", None)
        self._yt_gen = gen = getattr(self, "_yt_gen", 0) + 1

        def work():
            try:
                info, err = youtube.resolve(vid), None
            except Exception as e:
                info, err = None, str(e)
            GLib.idle_add(done, info, err)

        def done(info, err):
            if gen != self._yt_gen or self.root.get_visible_child_name() != "player":
                return False
            if err:
                self.set_buffering(None, None, None)
                dlg = Adw.AlertDialog(heading="Can't play this YouTube video here", body=err[:1:].upper() + err[1:])
                dlg.add_response("close", "Close")
                dlg.add_response("web", "Open on YouTube")
                dlg.set_response_appearance("web", Adw.ResponseAppearance.SUGGESTED)

                def resp(_d, r):
                    if r == "web":
                        Gio.AppInfo.launch_default_for_uri(youtube.watch_url(vid), None)
                    self.leave_player()
                dlg.connect("response", resp)
                dlg.present(self)
                return False
            self.p_title.set_label(info["title"])
            self.resume_key = f"yt:{vid}"
            saved = self._saved_pos(self.resume_key)
            m = self.video.mpv
            m.user_agent = info["ua"] or "Mozilla/5.0 (X11; Linux x86_64)"
            if info["live"]:
                self.live = True
                self._live_t = time.time()
                self.seek.set_visible(False)
            self.set_buffering("Starting…", f"{info['channel']} · {info['quality']}", None)
            self.doctor.start("url", info["url"], info["title"], refresh=lambda: youtube.resolve(vid)["url"])
            first = start if start is not None else saved
            self.video.play(info["url"], info["subs"][:1], start=None if info["live"] else first)
            if start is None and not info["live"]:
                self._offer_resume(saved)
            self._waiting_first_frame = time.time()
            self._add_history("youtube", vid, info["title"], info["thumb"])
            return False
        threading.Thread(target=work, daemon=True).start()

    def play_camera(self, cam):
        """Full-screen view of one of the user's cameras (main stream, the small one as backup)."""
        self.cameras.on_hidden()
        streams = [{"url": cam["main"]}] + ([{"url": cam["sub"]}] if cam.get("sub") else [])
        self.play_live({"id": "cam:" + cam["id"], "name": cam["name"], "streams": streams, "camera": True},
                       streams[0])

    def _select_track(self, prop, val):
        if prop == "sid":
            if isinstance(val, tuple) and val[0] == "ai":
                self._stop_translation()
                self._start_ai_subs(val[1])
                return
            self._stop_ai_subs()
            if isinstance(val, tuple):
                self._start_translation(val[1])
                return
            self._stop_translation()
        setattr(self.video.mpv, prop, val)

    # ── AI subtitles (Whisper, on this computer) ─────────────────────────
    def _ai_media(self):
        """The file or web address AI subtitles can read for what's playing (None for live TV / partial torrents)."""
        if self.live:
            return None
        if self.local_path and os.path.isfile(self.local_path):
            return self.local_path
        try:
            p = self.video.mpv.path or ""
        except Exception:
            return None
        if os.path.isfile(p) or (p.startswith("http") and "archive.org" in p):
            return p
        return None

    def _start_ai_subs(self, task):
        media = self._ai_media()
        if not media:
            self.toast("AI subtitles need a finished video file")
            return
        if not ai.ready():
            dlg = Adw.AlertDialog(
                heading="Download the AI subtitle pack?",
                body=("CineHaven listens to the film and writes subtitles itself, using OpenAI's Whisper "
                      f"speech recognition running on this computer – nothing is uploaded. The pack is {ai.PACK_SIZE} "
                      "and is downloaded once. It can also translate any language into English."))
            dlg.add_response("cancel", "Not now")
            dlg.add_response("go", "Download")
            dlg.set_response_appearance("go", Adw.ResponseAppearance.SUGGESTED)

            def resp(_d, r):
                if r != "go":
                    return
                self.toast("Downloading the AI subtitle pack – you can keep watching")

                def work():
                    try:
                        ai.install(lambda t: GLib.idle_add(self.toast, t))
                        GLib.idle_add(self._start_ai_subs, task)
                    except Exception as e:
                        health.log.error("AI pack install failed: %s", e)
                        GLib.idle_add(self.toast, "Couldn't install the AI subtitle pack – see Help & diagnostics")
                threading.Thread(target=work, daemon=True).start()
            dlg.connect("response", resp)
            dlg.present(self)
            return False
        self._stop_ai_subs()
        m = self.video.mpv
        pos = m.time_pos or 0
        self._ai_sid = None
        self._ai_done_to = pos
        media_now = media

        def update(path, info):
            GLib.idle_add(self._ai_update, media_now, path, info)

        def done(path, ok):
            GLib.idle_add(self._ai_finished, media_now, ok)
        self._ai = ai.Subtitler(media, pos, task, update, done)
        self._ai_media_now = media
        self.toast("✨ Writing subtitles – they'll appear in a few seconds")
        return False

    def _ai_update(self, media, path, info):
        if not getattr(self, "_ai", None) or self._ai.media != media:
            return False
        m = self.video.mpv
        self._ai_done_to = info.get("done_to", self._ai_done_to)
        if not info.get("cached") and not info.get("lines"):
            return False                         # nothing said yet – mpv can't load an empty file
        try:
            if self._ai_sid is None:
                m.command("sub-add", path, "select", "AI subtitles")
                tid = next((t["id"] for t in (m.track_list or []) if t.get("type") == "sub"
                            and t.get("external-filename") == path), None)
                self._ai_sid = tid
                if info.get("lang") and info["lang"] != "en" and self._ai.task == "translate":
                    self.toast(f"✨ Translating from {info['lang']} to English")
            else:
                m.command("sub-reload", self._ai_sid)
                m.sid = self._ai_sid
        except Exception as e:
            health.log.warning("Couldn't load AI subtitles: %s", e)
        return False

    def _ai_finished(self, media, ok):
        if getattr(self, "_ai", None) and self._ai.media == media and not ok:
            health.log.info("AI subtitles stopped before the end")
        return False

    def _stop_ai_subs(self):
        if getattr(self, "_ai", None):
            self._ai.stop()
        self._ai = None
        self._ai_sid = None

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
        if self.mini:
            self.mini_rev.set_reveal_child(True)
            self.set_cursor(None)
            if self.hide_src:
                GLib.source_remove(self.hide_src)
            self.hide_src = GLib.timeout_add(2200, self._auto_hide)
            return
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
        if self.mini:
            self.mini_rev.set_reveal_child(False)
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
        if self.mini:
            if n == 2:
                self.expand_player()
            else:
                self._show_controls()
            return
        if n == 2:
            self.toggle_fullscreen()
        self._show_controls()

    def _on_key(self, _c, keyval, _code, state):
        if self.mini == "window" and Gdk.keyval_name(keyval) == "Escape":
            self.expand_player()
            return True
        if self.root.get_visible_child_name() != "player" or self.mini == "corner":
            if keyval == Gdk.KEY_f and state & Gdk.ModifierType.CONTROL_MASK:
                self.views.set_visible_child_name("discover")
                self.search.grab_focus()
                return True
            if keyval in (Gdk.KEY_v, Gdk.KEY_V) and state & Gdk.ModifierType.CONTROL_MASK \
                    and not isinstance(self.get_focus(), (Gtk.Text, Gtk.TextView)):
                self._paste_link()                  # Ctrl+V anywhere (not in a text box): play the link
                return True
            return False
        k = Gdk.keyval_name(keyval)
        m = self.video.mpv
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if self.resume_box.get_visible():
            if k in ("space", "Return", "KP_Enter", "k", "p"):
                self._resume_choice(True)
                return True
            if k in ("Home", "0", "BackSpace", "r"):
                self._resume_choice(False)
                return True
        acts = {
            "space": self.toggle_pause, "k": self.toggle_pause, "p": self.toggle_pause,
            "f": self.toggle_fullscreen, "F11": self.toggle_fullscreen,
            "Left": lambda: self.nudge(-1) if shift else self.nudge(-5) if ctrl else self.seek_rel(-10),
            "Right": lambda: self.nudge(1) if shift else self.nudge(5) if ctrl else self.seek_rel(30),
            "comma": lambda: self.frame_step(-1), "period": lambda: self.frame_step(1),
            "j": lambda: self.seek_rel(-10), "l": lambda: self.seek_rel(10),
            "Up": lambda: self.nudge_volume(1 if shift else 5),
            "Down": lambda: self.nudge_volume(-1 if shift else -5),
            "m": lambda: setattr(m, "mute", not m.mute),
            "s": lambda: m.cycle("sub"), "v": lambda: m.cycle("sub"),
            "a": lambda: m.cycle("audio"), "b": lambda: m.cycle("audio"),
            "bracketleft": lambda: setattr(m, "speed", max(0.25, m.speed - 0.1)),
            "bracketright": lambda: setattr(m, "speed", min(4, m.speed + 0.1)),
            "BackSpace": lambda: setattr(m, "speed", 1.0),
            "z": lambda: (setattr(m, "sub_delay", round((m.sub_delay or 0) - 0.1, 2)),
                          m.show_text(f"Subtitle timing {m.sub_delay:+.1f} s")),
            "x": lambda: (setattr(m, "sub_delay", round((m.sub_delay or 0) + 0.1, 2)),
                          m.show_text(f"Subtitle timing {m.sub_delay:+.1f} s")),
        }
        if k == "Escape":
            self.unfullscreen() if self.is_fullscreen() else self.leave_player()
            return True
        if k in ("i", "I") and not ctrl:
            self.mini_player("window" if shift else "corner")
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
        if self.remote.running:
            self._remote_state()
        if self.root.get_visible_child_name() != "player":
            return True
        m = self.video.mpv
        if self.cast_dev:
            self._cast_tick()
            return True
        try:
            pos, dur = m.time_pos, m.duration
            paused, cache_wait = m.pause, m.paused_for_cache
        except Exception:
            return True
        if self.pending_play is None:
            self.doctor.tick(pos, paused, cache_wait)
        if pos and not paused and time.time() - self._last_pos_save > 15:
            self._last_pos_save = time.time()
            self._remember_position()
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
        if self.mini:
            self.mini_pp.set_icon_name("media-playback-start-symbolic" if paused else "media-playback-pause-symbolic")
            if pos is not None and dur:
                self.mini_prog.set_fraction(min(1, pos / dur))
        if not self.live:
            self._skip_tick(m, pos, dur, paused)
        self._sleep_tick(m, pos, dur, paused)
        try:
            key = (m.path, bool(m.video_params), bool(m.audio_params))
        except Exception:
            key = None
        if key and key != getattr(self, "_enh_key", None) and (key[1] or key[2]):
            if not key[1] or getattr(self, "_enh_key", (None,))[0] != key[0]:
                self._pic_level = None         # a new film: start from what was learnt for its kind
            elif getattr(self, "_pic_cls", None) is None:
                self._pic_level = None         # its picture only just appeared
            self._enh_key = key                # new file (or its streams just appeared)
            self._apply_enhancements()
            self._drops = (time.time(), m.frame_drop_count or 0)
        elif self.settings.get("enhance_picture") and pos and not paused and getattr(self, "_drops", None):
            t0, d0 = self._drops
            if time.time() - t0 > 10:            # every 10 s: too many dropped frames? lighten the load
                d = (m.frame_drop_count or 0) - d0
                self._drops = (time.time(), m.frame_drop_count or 0)
                lvl = getattr(self, "_pic_level", None) or 2
                if d > 12 and lvl > 1:
                    adapt.record_drop(self._pic_cls, lvl)
                    self._pic_level = lvl - 1
                    self._smooth_s = 0
                    self._apply_enhancements()
                    health.log.info("Enhance picture: dropped %d frames in 10 s at level %d (%s) – now %d",
                                    d, lvl, self._pic_cls, lvl - 1)
                    self.toast(f"Picture quality set to “{adapt.LEVELS[lvl - 1]}” to keep playback smooth – "
                               "remembered for films like this")
                elif d <= 2:
                    self._smooth_s = getattr(self, "_smooth_s", 0) + 10
                    if self._smooth_s >= 300:      # five smooth minutes: note it
                        adapt.record_smooth(self._pic_cls, lvl, self._smooth_s)
                        self._smooth_s = 0
        a = getattr(self, "_ai", None)
        reported = a is not None and self._ai_done_to > a.start_at       # worker has finished a piece
        if a and pos is not None and not os.path.exists(a.out + ".done") and (
                pos < a.start_at - 5                                       # jumped back before it
                or (reported and pos > self._ai_done_to + 20)              # jumped ahead of what's written
                or (not reported and pos > a.start_at + 90)):              # jumped far before the first piece
            task = a.task
            self._ai_done_to = pos
            sid = self._ai_sid
            a.stop()
            self._ai = ai.Subtitler(a.media, pos, task, lambda p, i, md=a.media: GLib.idle_add(self._ai_update, md, p, i),
                                    lambda p, ok, md=a.media: GLib.idle_add(self._ai_finished, md, ok))
            self._ai_sid = sid
        if pos is not None and not self._lang_done and pos > 0.5:
            self._lang_done = True
            if self.settings.get("auto_english", True):
                self._auto_language()
        if self.pending_play is None and pos is not None and not self.doctor.offline:
            if cache_wait:
                st = self.current.handle.status() if self.current else None
                rate = f"↓ {human_bytes(st.download_rate)}/s · {st.num_peers} peers" if st else ""
                self.set_buffering("Buffering…", rate, None)
            else:
                self.set_buffering(None, None, None)
        return True

    # ── cast to a TV ─────────────────────────────────────────────────────
    def _build_cast_popover(self, mb):
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=10, margin_bottom=10,
                      margin_start=10, margin_end=10)
        box.set_size_request(280, -1)
        box.append(Gtk.Label(label="Play on a TV", xalign=0, css_classes=["heading"]))
        status = Gtk.Label(label="Looking for TVs on your network…", xalign=0, wrap=True,
                           css_classes=["dim-label", "caption"])
        box.append(status)
        lst = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.append(lst)
        if self.cast_dev:
            stop = Gtk.Button(label=f"Stop casting to {self.cast_dev.name}", css_classes=["flat"])
            stop.connect("clicked", lambda *_: (pop.popdown(), self.stop_casting(resume_here=True)))
            box.append(stop)
        pop.set_child(box)
        mb.set_popover(pop)

        def found(devs):
            if not devs:
                status.set_label("No TVs found. Make sure the TV is on and on the same Wi-Fi. "
                                 "Apple TV and AirPlay TVs, Chromecast / Google TV, and DLNA smart TVs "
                                 "(Samsung, LG, Sony…) work."
                                 + ("" if cast.chromecast_available() else
                                    " Chromecast needs the pychromecast package."))
                return False
            status.set_label("Pick a TV:")
            for d in devs:
                b = Gtk.Button(css_classes=["flat"])
                b.set_child(Gtk.Label(label=f"📺  {d.name}  ·  {d.kind}", xalign=0))
                b.connect("clicked", lambda _b, d=d: (pop.popdown(), self.start_casting(d)))
                lst.append(b)
            return False
        threading.Thread(target=lambda: GLib.idle_add(found, cast.discover(5)), daemon=True).start()

    def _cast_source(self):
        """-> (url, mime) the TV can fetch for what's playing, or (None, reason)."""
        m = self.video.mpv
        try:
            path = m.path
        except Exception:
            path = None
        if not path:
            return None, "Nothing is playing"
        if path.startswith(("http://", "https://")):
            if "127.0.0.1" in path or "localhost" in path:
                return None, "This video is being converted on this computer and can't be sent to a TV yet"
            return path, ("application/x-mpegURL" if ".m3u8" in path else "video/mp4")
        if os.path.isfile(path):
            return cast.files.share(path), cast.mime_for(path)
        return None, "This video can't be sent to a TV"

    def _cast_do(self, fn, done=None):
        def work():
            try:
                fn()
                ok = None
            except Exception as e:
                ok = e
                health.log.info("Cast: %s", e)
            if done:
                GLib.idle_add(done, ok)
        threading.Thread(target=work, daemon=True).start()

    def start_casting(self, dev):
        url, mime = self._cast_source()
        if not url:
            self.toast(mime)
            return
        m = self.video.mpv
        pos = m.time_pos or 0
        title = self.p_title.get_label()
        try:
            vc = (m.video_codec or "").lower()
        except Exception:
            vc = ""
        m.pause = True
        if self.cast_dev and self.cast_dev is not dev:
            old = self.cast_dev
            self._cast_do(old.stop)
        self.cast_dev = dev
        self._cast_status = {"state": "BUFFERING", "pos": pos, "dur": m.duration}
        self.cast_lbl.set_label(f"Playing on {dev.name}")
        self.cast_sub.set_label("Starting on the TV…")
        self.cast_box.set_visible(True)
        self.buf_box.set_visible(False)
        self.skip_btn.set_visible(False)
        self.next_btn.set_visible(False)

        self._cast_offset = 0
        self._cast_playing = False
        if self._tv_stream:
            self._tv_stream.stop()
            self._tv_stream = None
        if dev.kind == "AirPlay":
            dev.ask_pin = lambda: self._ask_airplay_pin(dev)

        def done(err):
            if self.cast_dev is not dev:
                return False
            if err:
                self.cast_dev = None
                self.cast_box.set_visible(False)
                if self._tv_stream:
                    self._tv_stream.stop()
                    self._tv_stream = None
                self._cast_failed(dev, err)
                return False
            hint = ""
            if (dev.kind == "DLNA" and vc
                    and not any(k in vc for k in ("h264", "avc", "hevc", "h265", "vp9", "vp8", "av1"))):
                hint = " If the TV shows an error, it can't play this video format."
            self.cast_sub.set_label("Use the controls here or the phone remote." + hint)
            self._cast_playing = True
            return False

        local = url.startswith("http://") and os.path.isfile(getattr(self.video.mpv, "path", "") or "")
        path = self.video.mpv.path if local else None

        def work():
            try:
                if hasattr(dev, "connect"):
                    GLib.idle_add(lambda: self.cast_sub.set_label(f"Connecting to {dev.name}…") and False)
                    dev.connect()               # pair first – no point preparing a video it won't take
                info = fixvideo.probe(path) if path else None
                if path and cast.needs_conversion(info, dev.kind):
                    GLib.idle_add(lambda: self.cast_sub.set_label(
                        f"Preparing the video for {dev.name}…") and False)
                    ts = cast.TVStream(path, pos)
                    self._tv_stream = ts
                    got = {}
                    ev = threading.Event()
                    ts.run(lambda u: (got.update(url=u), ev.set()), lambda e: (got.update(err=e), ev.set()))
                    ev.wait(150)
                    if "url" not in got:
                        raise RuntimeError("couldn't prepare the video: " + str(got.get("err", "timed out")))
                    self._cast_offset = ts.start
                    dev.play(got["url"], title=title, mime="application/vnd.apple.mpegurl", start=0)
                else:
                    dev.play(url, title=title, mime=mime, start=pos)
                GLib.idle_add(done, None)
            except Exception as e:
                health.log.info("Cast to %s failed: %s", dev.name, e)
                GLib.idle_add(done, e)
        threading.Thread(target=work, daemon=True).start()

    def _cast_seek(self, pos):
        """Seek on the TV (positions there start where a prepared copy began)."""
        self.cast_dev.seek(max(0, pos - self._cast_offset))

    def _cast_failed(self, dev, err):
        msg = str(err)
        if dev.kind == "AirPlay" and ("403" in msg or "refused" in msg):
            body = (f"{dev.name} only accepts AirPlay from your own Apple devices right now.\n\n"
                    "To allow CineHaven:\n"
                    "• Apple TV: Settings → AirPlay and HomeKit → Allow Access → "
                    "“Anyone on the Same Network” (or “Everyone”).\n"
                    "• Mac: System Settings → General → AirDrop & Handoff → AirPlay Receiver → "
                    "Allow AirPlay for “Anyone on the Same Network”.\n"
                    "• Samsung / LG TV: Settings → General → Apple AirPlay Settings → Require Code → "
                    "“First Time Only”.\n\nThen press the TV button again.")
            d = Adw.AlertDialog(heading=f"{dev.name} needs a setting changed", body=body)
            d.add_response("ok", "OK")
            d.present(self)
            return
        if "cancelled" in msg:
            return
        self.toast(f"{dev.name} couldn't play this video")

    def _ask_airplay_pin(self, dev):
        """Called from a worker thread: show a dialog for the code on the TV, wait for the answer."""
        ev = threading.Event()
        answer = {}

        def show():
            d = Adw.AlertDialog(heading=f"Pair with {dev.name}",
                                body="Type the 4-digit code shown on the TV. CineHaven remembers it, "
                                     "so you only do this once.")
            entry = Gtk.Entry(input_purpose=Gtk.InputPurpose.DIGITS, max_length=4, xalign=0.5,
                              css_classes=["title-1"])
            d.set_extra_child(entry)
            d.add_response("cancel", "Cancel")
            d.add_response("pair", "Pair")
            d.set_response_appearance("pair", Adw.ResponseAppearance.SUGGESTED)
            d.set_default_response("pair")
            entry.connect("activate", lambda *_: (d.close(), resp(d, "pair")))

            def resp(_d, r):
                if not ev.is_set():
                    answer["pin"] = entry.get_text() if r == "pair" else None
                    ev.set()
            d.connect("response", resp)
            d.present(self)
            entry.grab_focus()
            return False
        GLib.idle_add(show)
        ev.wait(300)
        return answer.get("pin")

    def _cast_tick(self):
        st = self._cast_status
        if self._cast_playing and time.time() - self._cast_poll > 2:
            self._cast_poll = time.time()
            dev = self.cast_dev

            def work():
                try:
                    s = dev.status()
                except Exception:
                    return
                off = self._cast_offset
                if s.get("pos") is not None:
                    s["pos"] += off
                if s.get("dur") is not None and off:
                    s.pop("dur")            # a prepared copy grows as it converts – keep the film's length
                GLib.idle_add(lambda: (self.cast_dev is dev and self._cast_status.update(
                    {k: v for k, v in s.items() if v is not None})) and False)
            threading.Thread(target=work, daemon=True).start()
        pos, dur = st.get("pos"), st.get("dur")
        playing = st.get("state") in ("PLAYING", "BUFFERING")
        if playing and pos is not None:
            st["pos"] = pos + 0.25                      # tick between status polls
        self.pp.set_icon_name("media-playback-pause-symbolic" if playing else "media-playback-start-symbolic")
        if dur:
            self.seek.set_range(0, dur)
            self.seek.set_value(st.get("pos") or 0)
        self.time_lbl.set_label(f"{fmt_time(st.get('pos') or 0)} / {fmt_time(dur)}")

    def stop_casting(self, resume_here=True):
        dev = self.cast_dev
        if not dev:
            return
        pos = self._cast_status.get("pos")
        self.cast_dev = None
        self.cast_box.set_visible(False)
        self._cast_do(dev.stop)
        if self._tv_stream:
            self._tv_stream.stop()
            self._tv_stream = None
        cast.files.stop()
        if resume_here:
            try:
                if pos:
                    self.video.mpv.seek(pos, "absolute", "default-precise")
                self.video.mpv.pause = False
            except Exception:
                pass
            self.toast(f"Stopped casting to {dev.name}")

    # ── phone remote ─────────────────────────────────────────────────────
    # -- self-tuning ----------------------------------------------------------
    def _start_probe(self):
        def work():
            try:
                info = adapt.probe()
            except Exception as e:
                health.log.info("Video probe failed: %s", e)
                return
            GLib.idle_add(self._probe_done, info)
        threading.Thread(target=work, daemon=True).start()

    def _probe_done(self, info):
        tips = info.get("advice") or []
        seen = self.settings.get("tech_tip_seen")
        if tips and seen != info.get("fp"):
            self.settings["tech_tip_seen"] = info.get("fp")
            save_settings(self.settings)
            t = Adw.Toast(title=tips[0]["title"], timeout=12, button_label="Details")
            t.connect("button-clicked", lambda *_: self.show_tech())
            self.toasts.add_toast(t)
        return False

    def show_tech(self, force=False):
        d = Adw.Dialog(title="Video technology", content_width=620, content_height=640)
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        again = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Test again and look for news")
        hb.pack_start(again)
        tv.add_top_bar(hb)
        stack = Gtk.Stack()
        stack.add_named(Adw.StatusPage(title="Testing this computer…", child=Adw.Spinner()
                                       if hasattr(Adw, "Spinner") else Gtk.Spinner(spinning=True)), "wait")
        tv.set_content(stack)
        d.set_child(tv)
        d.present(self)

        def run(force):
            stack.set_visible_child_name("wait")
            again.set_sensitive(False)

            def work():
                try:
                    info = adapt.probe(force=force)
                except Exception as e:
                    info = {"error": str(e)}
                try:
                    news = adapt.tech_news(force=force)
                except Exception:
                    news = []
                GLib.idle_add(fill, info, news)
            threading.Thread(target=work, daemon=True).start()

        def fill(info, news):
            again.set_sensitive(True)
            old = stack.get_child_by_name("info")
            if old:
                stack.remove(old)
            page = Adw.PreferencesPage()
            if info.get("error"):
                g = Adw.PreferencesGroup(title="This computer")
                g.add(Adw.ActionRow(title="The test didn't finish", subtitle=info["error"]))
                page.add(g)

            tips = info.get("advice") or []
            if tips:
                g = Adw.PreferencesGroup(title="Suggestions")
                for tip in tips:
                    row = Adw.ActionRow(title=tip["title"], subtitle=tip["text"])
                    row.set_subtitle_lines(0)
                    if tip.get("package"):
                        b = Gtk.Button(label="Install", valign=Gtk.Align.CENTER, css_classes=["suggested-action"])

                        def install(btn, pkg=tip["package"]):
                            btn.set_sensitive(False)
                            btn.set_label("Installing…")

                            def w():
                                ok, msg = adapt.install_driver(pkg)
                                GLib.idle_add(lambda: (btn.set_label("Installed" if ok else "Install"),
                                                       btn.set_sensitive(not ok), self.toast(msg), False)[-1])
                            threading.Thread(target=w, daemon=True).start()
                        b.connect("clicked", install)
                        row.add_suffix(b)
                    g.add(row)
                    if tip.get("command"):
                        c = Adw.ActionRow(title="Or in a terminal", subtitle=f"sudo {tip['command']}",
                                          subtitle_selectable=True)
                        c.add_css_class("property")
                        g.add(c)
                page.add(g)

            g = Adw.PreferencesGroup(title="This computer")
            for vendor, _pid, name in info.get("gpus") or []:
                g.add(Adw.ActionRow(title="Graphics chip", subtitle=f"{vendor} {name}".strip()))
            g.add(Adw.ActionRow(title="Processor", subtitle=f"{info.get('cpu_threads', '?')} threads"))
            page.add(g)

            dec = info.get("decode") or {}
            g = Adw.PreferencesGroup(title="Who decodes each video format",
                                     description="Tested for real with short clips. The graphics chip is faster "
                                                 "and uses far less power than the processor.")
            if not dec:
                g.add(Adw.ActionRow(title="Couldn't test", subtitle="FFmpeg is needed to make the test clips"))
            for name, label, _a in adapt.CODECS:
                if name in dec:
                    hw = dec[name]
                    row = Adw.ActionRow(title=label, subtitle=f"Graphics chip ({hw})" if hw else "Processor")
                    row.add_prefix(Gtk.Image(icon_name="emblem-ok-symbolic" if hw else "dialog-information-symbolic"))
                    g.add(row)
            page.add(g)

            learnt = adapt.learned()
            g = Adw.PreferencesGroup(title="Picture quality it has learnt",
                                     description="With Enhance picture on, CineHaven starts every film at the best "
                                                 "quality it knows this computer can keep smooth, and steps down "
                                                 "by itself if frames drop. A faster setup is tried again.")
            if not learnt:
                g.add(Adw.ActionRow(title="Nothing yet", subtitle="It learns as you watch with Enhance picture on"))
            for cls, rec in sorted(learnt.items()):
                lvl = adapt.start_level(cls)
                bits = [f"starts at “{adapt.LEVELS[lvl]}”"]
                if rec.get("seconds"):
                    bits.append(f"{rec['seconds'] // 60} min smooth")
                if rec.get("fail"):
                    bits.append(f"stuttered at “{adapt.LEVELS.get(rec['fail'], rec['fail'])}”")
                g.add(Adw.ActionRow(title=adapt.describe_class(cls), subtitle=" · ".join(bits)))
            if learnt:
                f = Gtk.Button(label="Forget", valign=Gtk.Align.CENTER, css_classes=["flat"])
                f.connect("clicked", lambda *_: (adapt.forget(), self.toast("Forgotten – it will learn again"),
                                                 run(False)))
                g.set_header_suffix(f)
            page.add(g)

            g = Adw.PreferencesGroup(title="Video technology news",
                                     description="Checked once a week. " + adapt.how_to_update_engines())
            for r in news:
                if r["latest"] is None:
                    sub = f"You have {r['have']} – couldn't check for newer"
                elif r["newer"]:
                    sub = f"You have {r['have']} · {r['latest']} is out"
                else:
                    sub = f"{r['have']} – the newest"
                row = Adw.ActionRow(title=r["name"], subtitle=sub)
                if r["newer"] and r["url"]:
                    b = Gtk.Button(label="What's new", valign=Gtk.Align.CENTER, css_classes=["flat"])
                    b.connect("clicked", lambda *_, u=r["url"]: Gtk.UriLauncher.new(u).launch(self, None, None, None))
                    row.add_suffix(b)
                g.add(row)
            page.add(g)
            stack.add_named(page, "info")
            stack.set_visible_child_name("info")
            return False

        again.connect("clicked", lambda *_: run(True))
        run(force)

    def show_remote(self):
        try:
            url = self.remote.start()
            self._remote_state()
        except Exception as e:
            self.toast(f"Couldn't start the phone remote: {e}")
            return
        d = Adw.Dialog(title="Phone remote", content_width=420) if hasattr(Adw, "Dialog") else None
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_top=12, margin_bottom=24,
                      margin_start=24, margin_end=24)
        box.append(Gtk.Label(label="On your phone (on the same Wi-Fi), open this address in the browser:",
                             wrap=True, justify=Gtk.Justification.CENTER))
        tex = None
        try:
            tex = remote.qr_texture(url)
        except Exception:
            tex = None
        if tex:
            pic = Gtk.Picture.new_for_paintable(tex)
            pic.set_size_request(220, 220)
            pic.set_can_shrink(True)
            box.append(pic)
            box.append(Gtk.Label(label="…or scan this code with the phone's camera", css_classes=["dim-label"]))
        lbl = Gtk.Label(label=url, selectable=True, wrap=True, css_classes=["title-3"],
                        justify=Gtk.Justification.CENTER)
        box.append(lbl)
        copy = Gtk.Button(label="Copy address", css_classes=["pill"], halign=Gtk.Align.CENTER)
        copy.connect("clicked", lambda *_: (self.get_clipboard().set(url), self.toast("Address copied")))
        box.append(copy)
        box.append(Gtk.Label(label="The address changes each time CineHaven starts, so only people who can see "
                                   "this screen can use it. Tip: add it to the phone's home screen.",
                             wrap=True, css_classes=["dim-label", "caption"], justify=Gtk.Justification.CENTER))
        if d:
            tv = Adw.ToolbarView()
            tv.add_top_bar(Adw.HeaderBar())
            tv.set_content(box)
            d.set_child(tv)
            d.present(self)
        else:
            w = Adw.Window(title="Phone remote", transient_for=self, modal=True, content=box)
            w.present()

    def _remote_state(self):
        if self.root.get_visible_child_name() != "player":
            self.remote.update(title="", where="Pick something to watch on the computer")
            return
        m = self.video.mpv
        st = self._skip
        try:
            if self.cast_dev:
                c = self._cast_status
                pos, dur = c.get("pos"), c.get("dur")
                paused = c.get("state") not in ("PLAYING", "BUFFERING")
            else:
                pos, dur, paused = m.time_pos, m.duration, m.pause
            vol = self.vol.get_value()
        except Exception:
            return
        seg = st.get("cur")
        self.remote.update(
            title=self.p_title.get_label(), paused=bool(paused), pos=pos, dur=dur, vol=vol, live=self.live,
            where=f"Playing on {self.cast_dev.name}" if self.cast_dev else "Playing on the computer",
            skip=skip.LABEL.get(seg[0]) if seg and not self.cast_dev else None,
            next=bool(st.get("next")) and not self.cast_dev)

    def _remote_command(self, c, v):
        if self.root.get_visible_child_name() != "player":
            return False
        m = self.video.mpv
        try:
            if c == "toggle":
                self.toggle_pause()
            elif c == "back":
                self.seek_rel(-10)
            elif c == "fwd":
                self.seek_rel(30)
            elif c == "inch" and v is not None:
                self.nudge(max(-10, min(10, float(v))))
            elif c == "volstep" and v is not None:
                self.nudge_volume(max(-10, min(10, int(v))))
            elif c == "seek" and v is not None:
                self._on_seek(None, None, float(v))
            elif c == "vol" and v is not None:
                self.vol.set_value(max(0, min(150, float(v))))
            elif c == "skip":
                self._do_skip(manual=True)
            elif c == "next":
                self._play_next()
            elif c == "subs":
                m.cycle("sub")
            elif c == "audio":
                m.cycle("audio")
            elif c == "full":
                self.toggle_fullscreen()
            elif c == "stop":
                self.leave_player()
        except Exception as e:
            health.log.info("Remote: %s failed: %s", c, e)
        self._remote_state()
        return False

    # ── skip intro / credits, next episode ───────────────────────────────
    def _skip_tick(self, m, pos, dur, paused):
        st = self._skip
        try:
            path = m.path
        except Exception:
            path = None
        if path != st["path"]:                        # a new video: find what can be skipped
            self._skip = st = {"path": path, "segs": [], "done": set(), "next": None, "cd": None}
            self.skip_btn.set_visible(False)
            self.next_btn.set_visible(False)
            if path and dur:
                self._find_skips(path, m, dur)
            elif path:
                st["path"] = None                     # duration not known yet – try again next tick
            return
        if pos is None or not dur:
            return
        seg = next((sg for sg in st["segs"] if sg[1] <= pos < sg[2] - 1.5), None)
        st["cur"] = seg
        nxt = st["next"]
        if seg and seg[0] == "credits" and nxt:
            self.skip_btn.set_visible(False)
            self._offer_next(pos, dur, paused)
            return
        if not seg and nxt and dur - pos < 25:        # no credits found: offer it in the last seconds
            self._offer_next(pos, dur, paused)
            return
        if st["cd"] is not None:
            st["cd"] = None
            self.next_btn.set_visible(False)
        if not seg:
            self.skip_btn.set_visible(False)
            return
        key = (seg[0], round(seg[1]))
        if (self._setting("auto_skip") and seg[0] in ("intro", "recap") and key not in st["done"]
                and pos < seg[1] + 5 and not self.resume_box.get_visible()):
            self._do_skip(manual=False)
            return
        self.skip_btn.set_label(f"{skip.LABEL[seg[0]]}  ⏭")
        self.skip_btn.set_visible(True)

    def _find_skips(self, path, m, dur):
        try:
            chapters = m.chapter_list
        except Exception:
            chapters = None
        segs = skip.from_chapters(chapters, dur)
        local = os.path.isfile(path)
        nxt = skip.next_episode(path) if local else None
        self._skip.update(segs=segs, next=nxt)
        if local and skip.siblings(path) and not any(sg[0] in ("intro", "credits") for sg in segs):
            def work():
                found = skip.analyse(path)
                GLib.idle_add(done, found)

            def done(found):
                if self._skip["path"] == path:
                    have = {sg[0] for sg in self._skip["segs"]}
                    self._skip["segs"] += [tuple(f) for f in found if f[0] not in have]
                return False
            threading.Thread(target=work, daemon=True).start()

    def _do_skip(self, manual):
        st = self._skip
        seg = st.get("cur")
        if not seg:
            return
        st["done"].add((seg[0], round(seg[1])))
        self.skip_btn.set_visible(False)
        if seg[0] == "credits" and st["next"]:
            self._play_next()
            return
        try:
            back = self.video.mpv.time_pos
            self.video.mpv.seek(seg[2], "absolute", "exact")
        except Exception:
            return
        if not manual:
            t = Adw.Toast(title=f"Skipped the {seg[0]}", timeout=5, button_label="Watch it")
            t.connect("button-clicked", lambda *_: self.video.mpv.seek(max(seg[1], (back or seg[1]) - 1), "absolute", "exact"))
            self.toasts.add_toast(t)

    def _offer_next(self, pos, dur, paused):
        st = self._skip
        name = os.path.splitext(os.path.basename(st["next"]))[0]
        if not self._setting("autoplay_next") or (self._sleep and self._sleep[0] == "end"):
            self.next_btn.set_label("Next episode ▶")
            self.next_btn.set_tooltip_text(name)
            self.next_btn.set_visible(True)
            return
        if st["cd"] is None:
            st["cd"] = time.time() + 10
        if paused:
            st["cd"] = time.time() + 10               # hold the countdown while paused
        left = max(0, int(st["cd"] - time.time() + 0.99))
        if left == 0 or dur - pos < 1.5:
            self._play_next()
            return
        self.next_btn.set_label(f"Next episode in {left} s ▶")
        self.next_btn.set_tooltip_text(name)
        self.next_btn.set_visible(True)

    def _play_next(self):
        nxt = self._skip.get("next")
        self.next_btn.set_visible(False)
        if not nxt:
            return
        self._skip["next"] = None
        title = self._episode_title(nxt)
        self.play_local(nxt, title)

    def _episode_title(self, path):
        try:
            from .localmedia import parse
            info = parse(path)
            t = info.get("title") or os.path.basename(path)
            if info.get("season") is not None and info.get("episode") is not None:
                t += f" · S{info['season']:02d}E{info['episode']:02d}"
            return t
        except Exception:
            return os.path.basename(path)

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
                self.myvideos.schedule_rescan(2)            # show it in My Videos right away
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
        self.myvideos.schedule_rescan(2)                    # show it in My Videos right away
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
        self._stop_ai_subs()
        self._remember_position()
        self.enter_player(title or os.path.basename(path), None)
        self.set_buffering(None, None, None)
        self.pending_play = None
        self._reset_live()
        self._stop_translation()
        self._lang_done = False
        self.local_path = path
        self.resume_key = path
        start = self._saved_pos(path)
        self.doctor.start("local", path, title or os.path.basename(path))
        self.video.play(path, start=start)
        self._add_history("file", path, title or os.path.splitext(os.path.basename(path))[0])
        self._offer_resume(start)

    def _saved_pos(self, key):
        e = self.settings.get("resume", {}).get(key) if key else None
        return e.get("t") if e else None

    def _remember_position(self):
        """Save where the user is, so the video can continue there next time."""
        key = self.resume_key
        if not key or self.resume_box.get_visible():      # don't overwrite while the prompt is up
            return
        try:
            pos, dur = self.video.mpv.time_pos, self.video.mpv.duration
        except Exception:
            return
        res = self.settings.setdefault("resume", {})
        if pos and dur and 30 < pos < dur - 120:
            res[key] = {"t": round(pos, 1), "dur": round(dur, 1), "when": time.time(),
                        "title": self.p_title.get_label()}
        elif pos and dur and pos >= dur - 120:
            res.pop(key, None)            # finished – start from the beginning next time
        for e in self.settings.get("history", []):
            if e["key"] == key and pos and dur:
                e.update(t=round(pos, 1), dur=round(dur, 1), when=time.time(), done=pos >= dur - 120)
        if len(res) > 500:                # keep the newest 500
            for k in sorted(res, key=lambda k: res[k].get("when", 0))[:-500]:
                res.pop(k)
        save_settings(self.settings)

    # ── resume prompt ────────────────────────────────────────────────────
    def _offer_resume(self, start):
        self._hide_resume()
        if not start:
            return
        self.video.mpv.pause = True              # wait at the saved spot until the user chooses
        e = self.settings.get("resume", {}).get(self.resume_key) or {}
        dur = e.get("dur")
        self.resume_btn.set_label(f"▶  Resume from {fmt_time(start)}")
        when = e.get("when")
        ago = ""
        if when:
            d = time.time() - when
            ago = ("earlier today" if d < 86400 else "yesterday" if d < 172800
                   else f"{int(d // 86400)} days ago")
        self.resume_info.set_label(f"You stopped at {fmt_time(start)}" + (f" of {fmt_time(dur)}" if dur else "")
                                   + (f" · {ago}" if ago else ""))
        self.resume_box.set_visible(True)
        self.resume_btn.grab_focus()
        self._resume_left = 10

        def tick():
            self._resume_left -= 1
            if self._resume_left <= 0:
                self._resume_cd = None
                self._resume_choice(True)
                return False
            self.resume_cd_lbl.set_label(f"Resuming automatically in {self._resume_left} s")
            return True
        self.resume_cd_lbl.set_label(f"Resuming automatically in {self._resume_left} s")
        self._resume_cd = GLib.timeout_add_seconds(1, tick)

    def _hide_resume(self):
        if self._resume_cd:
            GLib.source_remove(self._resume_cd)
            self._resume_cd = None
        self.resume_box.set_visible(False)

    def _resume_choice(self, resume):
        if not self.resume_box.get_visible():
            return
        self._hide_resume()
        m = self.video.mpv
        if not resume:
            self.settings.get("resume", {}).pop(self.resume_key, None)
            save_settings(self.settings)
            try:
                m.seek(0, "absolute", "default-precise")
            except Exception:
                m.command("loadfile", m.path or "", "replace")
            self.toast("Starting from the beginning")
        m.pause = False
        self._show_controls()

    def forget_resume(self, path):
        self.settings.get("resume", {}).pop(path, None)
        save_settings(self.settings)

    def _paste_link(self):
        def got(c, res):
            try:
                t = (c.read_text_finish(res) or "").strip()
            except GLib.Error:
                t = ""
            if youtube.is_link(t) or normalize_magnet(t):
                self.open_any(t)
            elif t:
                self.toast("Copy a YouTube or magnet link, then press Ctrl+V to play it")
        self.get_clipboard().read_text_async(None, got)

    def ask_youtube(self, *_):
        dlg = Adw.AlertDialog(heading="Play a YouTube link",
                              body="Paste the address of any YouTube video. It plays here in CineHaven.")
        entry = Gtk.Entry(placeholder_text="https://www.youtube.com/watch?v=…", activates_default=True)

        def got(c, res):
            try:
                t = (c.read_text_finish(res) or "").strip()
                if youtube.is_link(t):
                    entry.set_text(t)
            except GLib.Error:
                pass
        self.get_clipboard().read_text_async(None, got)
        dlg.set_extra_child(entry)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("play", "Play")
        dlg.set_response_appearance("play", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("play")

        def resp(_d, r):
            if r == "play":
                self.play_youtube(entry.get_text())
        dlg.connect("response", resp)
        dlg.present(self)

    def open_any(self, arg):
        if youtube.is_link(arg) and not os.path.exists(arg):
            self.play_youtube(arg)
        elif normalize_magnet(arg) and not os.path.exists(arg):
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
        self.together.shutdown()
        if self.cast_dev:
            try:
                self.cast_dev.stop()
            except Exception:
                pass
        cast.files.stop()
        self.remote.stop()
        self.mpris.stop()
        self._remember_position()
        save_settings(self.settings)
        self._stop_translation()
        self.tr_pool.shutdown(wait=False, cancel_futures=True)
        self.livetv.shutdown()
        self.cameras.shutdown()
        self.myvideos.shutdown()
        self.video.shutdown()
        self.thumbs.shutdown(wait=False, cancel_futures=True)


# ──────────────────────────────────────────────────────────────────────────
class _Modes:
    """Stands in for the old browse/player Stack: which of the two is showing."""

    def __init__(self, win):
        self.win = win
        self.name = "browse"

    def get_visible_child_name(self):
        return self.name

    def set_visible_child_name(self, name):
        self.win._set_mode(name)


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        self.engine = None
        self.settings = load_settings()
        GLib.set_application_name(APP_NAME)

    def do_startup(self):
        Adw.Application.do_startup(self)
        css = Gtk.CssProvider()
        data = CSS + MYLIST_CSS + LIVE_CSS + LOCAL_CSS + YT_CSS + CAM_CSS + TOGETHER_CSS
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
            if uri.lower().startswith("magnet:") or youtube.is_link(uri):
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

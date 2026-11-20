"""YouTube page: free full movies from official channels, plus the user's own picks."""
import threading

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk, Pango  # noqa: E402

from . import youtube  # noqa: E402

CSS = b"""
.yt-card { padding: 6px; border-radius: 14px; }
.yt-card:hover { background: alpha(currentColor,.07); }
.yt-thumb { border-radius: 10px; }
.yt-official { color: @success_color; font-weight: 700; font-size: .8em; }
.yt-unofficial { color: @warning_color; font-weight: 700; font-size: .8em; }
"""

SECTIONS = [("all", "🎬 All films"), ("heartfelt", "💛 Heartfelt"), ("picks", "⭐ My picks")]


class _Thumb(Gtk.Picture):
    """A 16:9 picture of fixed size (a plain Gtk.Picture grows to the image's natural size)."""

    def __init__(self, w, h):
        super().__init__(content_fit=Gtk.ContentFit.COVER, can_shrink=True, css_classes=["yt-thumb"],
                         halign=Gtk.Align.START)
        self._w, self._h = w, h
        self.set_overflow(Gtk.Overflow.HIDDEN)

    def do_measure(self, orientation, _for_size):
        n = self._w if orientation == Gtk.Orientation.HORIZONTAL else self._h
        return n, n, -1, -1


class FilmCard(Gtk.Box):
    def __init__(self, win, f):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4, css_classes=["yt-card"],
                         valign=Gtk.Align.START)
        self.f = f
        pic = _Thumb(228, 128)
        win.load_thumb(f["thumb"], pic)
        self.append(pic)
        t = Gtk.Label(label=f["title"], wrap=True, lines=2, xalign=0, max_width_chars=24, width_chars=24,
                      ellipsize=Pango.EllipsizeMode.END, css_classes=["heading"])
        t.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.append(t)
        row = Gtk.Box(spacing=6)
        official = f.get("official", True)
        row.append(Gtk.Label(label="✔ Official" if official else "⚠ Unverified",
                             css_classes=["yt-official" if official else "yt-unofficial"]))
        row.append(Gtk.Label(label=f["channel"], ellipsize=Pango.EllipsizeMode.END,
                             css_classes=["dim-label", "caption"]))
        self.append(row)


class YouTubePage(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.all = None
        self.section = win.settings.get("yt_section", "all")
        self._loading = False

        top = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        chips = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
        group = None
        for key, label in SECTIONS:
            b = Gtk.ToggleButton(label=label, group=group, active=(key == self.section), css_classes=["pill"])
            group = group or b
            b.connect("toggled", lambda btn, k=key: btn.get_active() and self.show_section(k))
            chips.append(b)
        top.append(chips)

        row = Gtk.Box(spacing=6)
        self.filter = Gtk.SearchEntry(placeholder_text="Filter by title…", hexpand=True)
        self.filter.connect("search-changed", lambda *_: self.flow.invalidate_filter())
        row.append(self.filter)
        self.link = Gtk.Entry(placeholder_text="Paste a YouTube link to add it to My picks", hexpand=True)
        self.link.connect("activate", lambda *_: self.add_link())
        row.append(self.link)
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add to My picks", css_classes=["suggested-action"])
        add.connect("clicked", lambda *_: self.add_link())
        row.append(add)
        c = Adw.Clamp(maximum_size=900)
        c.set_child(row)
        top.append(c)
        self.status = Gtk.Label(css_classes=["dim-label"], wrap=True, justify=Gtk.Justification.CENTER)
        top.append(self.status)
        clamp = Adw.Clamp(maximum_size=1100, margin_top=16, margin_start=16, margin_end=16, margin_bottom=6)
        clamp.set_child(top)
        self.append(clamp)

        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True, valign=Gtk.Align.START,
                                max_children_per_line=8, min_children_per_line=1, column_spacing=8,
                                row_spacing=12, margin_start=16, margin_end=16, margin_bottom=24,
                                activate_on_single_click=True)
        self.flow.set_filter_func(self._filter)
        self.flow.connect("child-activated", lambda fb, ch: self.show_details(ch.get_child().f))
        scw = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scw.set_child(self.flow)
        self.append(scw)
        self._loaded = False

    # ── loading ──────────────────────────────────────────────────────────
    def on_shown(self):
        if not self._loaded and not self._loading:
            self._loading = True
            self.status.set_label("Loading free films from official YouTube channels…")

            def work():
                try:
                    res = youtube.films()
                except Exception as e:
                    res = e
                GLib.idle_add(self._ready, res)
            threading.Thread(target=work, daemon=True).start()

    def _ready(self, res):
        self._loading = False
        if isinstance(res, Exception):
            self.all = []
            self.status.set_label("Couldn't reach YouTube – check your internet connection. Your picks are still here.")
        else:
            self._loaded = True
            self.all = res
        self.show_section(self.section, keep_status=isinstance(res, Exception))
        return False

    def _filter(self, child):
        q = self.filter.get_text().strip().lower()
        return not q or q in child.get_child().f["title"].lower()

    def show_section(self, key, keep_status=False):
        self.section = key
        self.win.settings["yt_section"] = key
        self.flow.remove_all()
        if key == "picks":
            items = youtube.picks()
        elif self.all is None:
            return
        else:
            items = [f for f in self.all if key == "all" or f["heartfelt"]]
        for f in items:
            self.flow.append(FilmCard(self.win, f))
        if keep_status:
            return
        if key == "picks":
            self.status.set_label(f"{len(items)} saved picks · paste a YouTube link above to add one"
                                  if items else "No picks yet – paste a YouTube link above to add a film you found")
        else:
            self.status.set_label(f"{len(items)} free films from official studio channels · "
                                  "they play on YouTube with ads, as the rights holders intend")

    # ── picks ────────────────────────────────────────────────────────────
    def add_link(self):
        text = self.link.get_text().strip()
        if not text:
            return
        self.link.set_sensitive(False)

        def work():
            try:
                res = youtube.add_pick(text)
            except ValueError as e:
                res = e
            GLib.idle_add(done, res)

        def done(res):
            self.link.set_sensitive(True)
            if isinstance(res, Exception):
                self.win.toast(str(res))
                return False
            self.link.set_text("")
            note = "" if res["official"] else " – not from a known official channel"
            self.win.toast(f"Added “{res['title']}” to My picks{note}")
            if self.section == "picks":
                self.show_section("picks")
            return False
        threading.Thread(target=work, daemon=True).start()

    # ── details ──────────────────────────────────────────────────────────
    def show_details(self, f):
        is_pick = any(p["id"] == f["id"] for p in youtube.picks())
        d = Adw.Dialog(title=f["title"], content_width=560)
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_start=24, margin_end=24,
                      margin_bottom=24, margin_top=6)
        pic = _Thumb(512, 288)
        self.win.load_thumb(f["thumb"], pic)
        col.append(pic)
        col.append(Gtk.Label(label=f["title"], wrap=True, xalign=0, css_classes=["hero-title"]))
        meta = [str(f["year"]) if f.get("year") else None, f["channel"]]
        col.append(Gtk.Label(label=" · ".join(m for m in meta if m), xalign=0, css_classes=["dim-label"]))
        if f.get("official", True):
            note = "Posted free by the film's rights holder. Plays on YouTube with ads."
        else:
            note = ("This channel isn't on CineHaven's list of official studio channels. "
                    "Only watch it if you know the uploader owns the film.")
        col.append(Gtk.Label(label=note, wrap=True, xalign=0, css_classes=["caption", "dim-label"]))
        if f.get("description"):
            col.append(Gtk.Label(label=f["description"], wrap=True, xalign=0, lines=6,
                                 ellipsize=Pango.EllipsizeMode.END))
        btns = Gtk.Box(spacing=8, margin_top=8)
        watch = Gtk.Button(label="▶  Watch on YouTube", css_classes=["suggested-action", "pill"])
        watch.connect("clicked", lambda *_: (d.close(), self.open_video(f)))
        btns.append(watch)
        if is_pick:
            rm = Gtk.Button(label="Remove from My picks", css_classes=["pill", "destructive-action"])
            rm.connect("clicked", lambda *_: (d.close(), self.remove(f)))
            btns.append(rm)
        else:
            save = Gtk.Button(label="Save to My picks", css_classes=["pill"])
            save.connect("clicked", lambda *_: (d.close(), self.save(f)))
            btns.append(save)
        col.append(btns)
        sc = Gtk.ScrolledWindow(propagate_natural_height=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        sc.set_child(col)
        tv.set_content(sc)
        d.set_child(tv)
        d.present(self.win)
        watch.grab_focus()

    def open_video(self, f):
        try:
            Gio.AppInfo.launch_default_for_uri(youtube.watch_url(f["id"]), None)
            self.win.toast(f"Opening “{f['title']}” in your browser")
        except GLib.Error:
            self.win.toast("Couldn't open your web browser")

    def save(self, f):
        self.link.set_text(youtube.watch_url(f["id"]))
        self.add_link()

    def remove(self, f):
        youtube.remove_pick(f["id"])
        self.win.toast(f"Removed “{f['title']}”")
        if self.section == "picks":
            self.show_section("picks")

"""YouTube page: search all of YouTube or paste a link (plays inside CineHaven), free full movies
from official channels, and the user's own picks."""
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
.yt-len { background: alpha(black,.75); color: white; border-radius: 6px; padding: 0 5px; font-size: .8em;
          font-weight: 700; margin: 0 6px 6px 0; }
.yt-live { background: #cc0000; }
.yt-search { font-size: 1.15em; min-height: 44px; }
"""

SECTIONS = [("results", "🔎 Search results"), ("all", "🎬 Free films"), ("heartfelt", "💛 Heartfelt"),
            ("picks", "⭐ My picks")]


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
        if f.get("search") and (f.get("length") or f.get("live")):
            ov = Gtk.Overlay()
            ov.set_child(pic)
            ov.add_overlay(Gtk.Label(label="LIVE" if f.get("live") else f["length"], halign=Gtk.Align.END,
                                     valign=Gtk.Align.END,
                                     css_classes=["yt-len"] + (["yt-live"] if f.get("live") else [])))
            self.append(ov)
        else:
            self.append(pic)
        t = Gtk.Label(label=f["title"], wrap=True, lines=2, xalign=0, max_width_chars=24, width_chars=24,
                      ellipsize=Pango.EllipsizeMode.END, css_classes=["heading"])
        t.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.append(t)
        row = Gtk.Box(spacing=6)
        official = f.get("official", True)
        if f.get("search"):
            bits = [f["channel"], f.get("views"), f.get("age")]
            row.append(Gtk.Label(label=" · ".join(b for b in bits if b), ellipsize=Pango.EllipsizeMode.END,
                                 max_width_chars=30, xalign=0, css_classes=["dim-label", "caption"]))
        else:
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

        if self.section == "results":
            self.section = "all"
        self.results = []
        top = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.query = Gtk.SearchEntry(placeholder_text="Search YouTube – or paste a YouTube link to play it",
                                     hexpand=True, search_delay=200, css_classes=["yt-search"])
        self.query.connect("activate", lambda e: self.go(e.get_text()))
        self.query.connect("search-changed", lambda e: self._suggest(e.get_text()))
        self.query.connect("stop-search", lambda *_: self.sugg_pop.popdown())
        paste = Gtk.Button(icon_name="edit-paste-symbolic", tooltip_text="Paste a link and play it",
                           css_classes=["flat"])
        paste.connect("clicked", lambda *_: self._paste())
        row = Gtk.Box(spacing=6)
        row.append(self.query)
        row.append(paste)
        c = Adw.Clamp(maximum_size=760)
        c.set_child(row)
        top.append(c)
        self.sugg_pop = Gtk.Popover(autohide=False, has_arrow=False, position=Gtk.PositionType.BOTTOM,
                                    halign=Gtk.Align.START, can_focus=False)
        self.sugg_pop.set_parent(self.query)
        self.sugg_list = Gtk.ListBox(css_classes=["navigation-sidebar"], can_focus=False)
        self.sugg_list.connect("row-activated", lambda lb, r: self.go(r.text))
        self.sugg_pop.set_child(self.sugg_list)
        self._sugg_gen = 0

        chips = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
        group = None
        self.chips = {}
        for key, label in SECTIONS:
            b = Gtk.ToggleButton(label=label, group=group, active=(key == self.section), css_classes=["pill"])
            group = group or b
            b.connect("toggled", lambda btn, k=key: btn.get_active() and self.show_section(k))
            self.chips[key] = b
            chips.append(b)
        self.chips["results"].set_visible(False)
        top.append(chips)
        self.status = Gtk.Label(css_classes=["dim-label"], wrap=True, justify=Gtk.Justification.CENTER)
        top.append(self.status)
        clamp = Adw.Clamp(maximum_size=1100, margin_top=16, margin_start=16, margin_end=16, margin_bottom=6)
        clamp.set_child(top)
        self.append(clamp)

        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True, valign=Gtk.Align.START,
                                max_children_per_line=8, min_children_per_line=1, column_spacing=8,
                                row_spacing=12, margin_start=16, margin_end=16, margin_bottom=24,
                                activate_on_single_click=True)
        self.flow.connect("child-activated", lambda fb, ch: self.activate(ch.get_child().f))
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

    # ── search and links ─────────────────────────────────────────────────
    def go(self, text):
        """Enter in the box: a link plays at once, anything else searches YouTube."""
        text = (text or "").strip()
        self.sugg_pop.popdown()
        self._sugg_gen += 1
        if not text:
            return
        if youtube.is_link(text) or youtube.video_id(text) and len(text) == 11 and " " not in text:
            self.win.play_youtube(text)
            return
        if self.query.get_text().strip() != text:
            self.query.set_text(text)
            self.sugg_pop.popdown()
            self._sugg_gen += 1
        self.chips["results"].set_visible(True)
        self.chips["results"].set_active(True)
        self.flow.remove_all()
        self.status.set_label(f"Searching YouTube for “{text}”…")
        gen = self._search_gen = getattr(self, "_search_gen", 0) + 1

        def work():
            try:
                res = youtube.search_all(text)
            except Exception as e:
                res = e
            GLib.idle_add(done, res)

        def done(res):
            if gen != self._search_gen:
                return False
            if isinstance(res, Exception):
                self.results = []
                self.status.set_label("Couldn't reach YouTube – check your internet connection")
                return False
            self.results = res
            self.results_query = text
            if self.section == "results":
                self.show_section("results")
            return False
        threading.Thread(target=work, daemon=True).start()

    def _paste(self):
        clip = self.win.get_clipboard()

        def got(c, res):
            try:
                text = c.read_text_finish(res) or ""
            except GLib.Error:
                text = ""
            if youtube.is_link(text):
                self.query.set_text(text.strip())
                self.go(text)
            else:
                self.win.toast("There's no YouTube link on the clipboard – copy one first")
        clip.read_text_async(None, got)

    def _suggest(self, text):
        text = text.strip()
        self._sugg_gen += 1
        gen = self._sugg_gen
        if len(text) < 2 or youtube.is_link(text) or not self.query.has_focus() and not self.query.get_focus_child():
            self.sugg_pop.popdown()
            return

        def work():
            res = youtube.suggest(text)
            GLib.idle_add(show, res)

        def show(res):
            if gen != self._sugg_gen:
                return False
            self.sugg_list.remove_all()
            for t in res:
                r = Gtk.ListBoxRow(can_focus=False)
                r.text = t
                r.set_child(Gtk.Label(label=t, xalign=0, margin_start=6, margin_end=6, margin_top=4,
                                      margin_bottom=4))
                self.sugg_list.append(r)
            self.sugg_pop.set_size_request(max(300, self.query.get_width()), -1)
            if res:
                self.sugg_pop.popup()
            else:
                self.sugg_pop.popdown()
            return False
        threading.Thread(target=work, daemon=True).start()

    def activate(self, f):
        if f.get("search"):
            self.win.play_youtube(f["id"], f["title"])
        else:
            self.show_details(f)

    def show_section(self, key, keep_status=False):
        self.section = key
        self.win.settings["yt_section"] = key
        self.flow.remove_all()
        if key == "results":
            items = self.results
        elif key == "picks":
            items = youtube.picks()
        elif self.all is None:
            return
        else:
            items = [f for f in self.all if key == "all" or f["heartfelt"]]
        for f in items:
            self.flow.append(FilmCard(self.win, f))
        if keep_status:
            return
        if key == "results":
            self.status.set_label(f"{len(items)} videos for “{getattr(self, 'results_query', '')}” · click one to play it"
                                  if items else "Nothing found – try other words")
        elif key == "picks":
            self.status.set_label(f"{len(items)} saved picks" if items else
                                  "No picks yet – open a film or search result and choose “Save to My picks”")
        else:
            self.status.set_label(f"{len(items)} free films from official studio channels · "
                                  "click one to watch it here")

    # ── picks ────────────────────────────────────────────────────────────
    def _add(self, text):

        def work():
            try:
                res = youtube.add_pick(text)
            except ValueError as e:
                res = e
            GLib.idle_add(done, res)

        def done(res):
            if isinstance(res, Exception):
                self.win.toast(str(res))
                return False
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
            note = "Posted free by the film's rights holder."
        else:
            note = ("This channel isn't on CineHaven's list of official studio channels. "
                    "Only watch it if you know the uploader owns the film.")
        col.append(Gtk.Label(label=note, wrap=True, xalign=0, css_classes=["caption", "dim-label"]))
        if f.get("description"):
            col.append(Gtk.Label(label=f["description"], wrap=True, xalign=0, lines=6,
                                 ellipsize=Pango.EllipsizeMode.END))
        btns = Gtk.Box(spacing=8, margin_top=8)
        watch = Gtk.Button(label="▶  Play", css_classes=["suggested-action", "pill"])
        watch.connect("clicked", lambda *_: (d.close(), self.win.play_youtube(f["id"], f["title"])))
        btns.append(watch)
        web = Gtk.Button(label="Open on YouTube", css_classes=["pill"])
        web.connect("clicked", lambda *_: (d.close(), self.open_video(f)))
        btns.append(web)
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
        self._add(youtube.watch_url(f["id"]))

    def remove(self, f):
        youtube.remove_pick(f["id"])
        self.win.toast(f"Removed “{f['title']}”")
        if self.section == "picks":
            self.show_section("picks")

"""Sport page: live scores, fixtures and results, with legal places to watch."""
import threading

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk, Pango  # noqa: E402

from . import sport  # noqa: E402

CSS = b"""
.match-card { padding: 12px 16px; border-radius: 14px; background: alpha(currentColor,.05); }
.match-card:hover { background: alpha(currentColor,.10); }
.match-card.live { box-shadow: inset 3px 0 #e5383b; }
.match-score { font-size: 1.6em; font-weight: 800; font-feature-settings: "tnum"; }
"""

LIVE_REFRESH = 30          # seconds, while a match is in play
IDLE_REFRESH = 300


class _Crest(Gtk.Image):
    def __init__(self):
        super().__init__(pixel_size=40, icon_name="emblem-default-symbolic")

    def set_paintable(self, tex):
        self.set_from_paintable(tex)


class MatchCard(Gtk.Box):
    def __init__(self, page, m):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8, css_classes=["match-card"])
        self.m = m
        if m["state"] == "in":
            self.add_css_class("live")
        top = Gtk.Box(spacing=6)
        if m["state"] == "in":
            top.append(Gtk.Label(label="● LIVE", css_classes=["live-dot"]))
        top.append(Gtk.Label(label=f'{m["league"]} · {m["status"]}', css_classes=["dim-label", "caption"],
                             ellipsize=Pango.EllipsizeMode.END, xalign=0, hexpand=True))
        self.append(top)

        row = Gtk.Box(spacing=12)
        for i, side in enumerate((m["home"], m["away"])):
            crest = _Crest()
            if side["logo"]:
                page.win.load_thumb(side["logo"], crest)
            name = Gtk.Label(label=side["name"], css_classes=["heading"], wrap=True, hexpand=True,
                             xalign=1 if i == 0 else 0, max_width_chars=18)
            if i == 0:
                row.append(name)
                row.append(crest)
                score = (f'{m["home"]["score"]}  –  {m["away"]["score"]}' if m["state"] != "pre" else "vs")
                row.append(Gtk.Label(label=score, css_classes=["match-score"], width_chars=9))
            else:
                row.append(crest)
                row.append(name)
        self.append(row)

        btns = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
        if m["state"] != "post":
            w = Gtk.MenuButton(label="Where to watch", css_classes=["pill", "suggested-action"] if
                               m["state"] == "in" else ["pill"])
            pop = Gtk.Popover()
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=6, margin_bottom=6,
                          margin_start=6, margin_end=6)
            for label, url, note in sport.where_to_watch(m, page.home):
                b = Gtk.Button(css_classes=["flat"])
                bl = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                bl.append(Gtk.Label(label=label, xalign=0))
                bl.append(Gtk.Label(label=note, xalign=0, css_classes=["dim-label", "caption"]))
                b.set_child(bl)
                b.connect("clicked", lambda *_, u=url, p=pop: (p.popdown(), page.open_url(u)))
                box.append(b)
            pop.set_child(box)
            w.set_popover(pop)
            btns.append(w)
        if m["state"] != "pre":
            h = Gtk.Button(label="Highlights", css_classes=["pill"])
            h.connect("clicked", lambda *_: page.highlights(m))
            btns.append(h)
        if m["link"]:
            s = Gtk.Button(label="Match centre", css_classes=["pill", "flat"],
                           tooltip_text="Line-ups, stats and commentary on ESPN")
            s.connect("clicked", lambda *_: page.open_url(m["link"]))
            btns.append(s)
        self.append(btns)


class SportPage(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        from .mylist import home_countries
        self.home = home_countries(win.settings)
        self.sport = win.settings.get("sport_section") or sport.SPORTS[0][0]
        self.gen = 0
        self._timer = None
        self._visible = False

        top = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        chips = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
        group = None
        for key, label, _l in sport.SPORTS:
            b = Gtk.ToggleButton(label=label, group=group, active=(key == self.sport), css_classes=["pill"])
            group = group or b
            b.connect("toggled", lambda btn, k=key: btn.get_active() and self.show(k))
            chips.append(b)
        rb = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh scores", css_classes=["flat"])
        rb.connect("clicked", lambda *_: self.refresh())
        chips.append(rb)
        top.append(chips)
        self.status = Gtk.Label(css_classes=["dim-label"], wrap=True, justify=Gtk.Justification.CENTER)
        top.append(self.status)
        clamp = Adw.Clamp(maximum_size=900, margin_top=16, margin_start=16, margin_end=16, margin_bottom=6)
        clamp.set_child(top)
        self.append(clamp)

        self.list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        lc = Adw.Clamp(maximum_size=760, margin_start=16, margin_end=16, margin_bottom=16)
        lc.set_child(self.list)
        foot = Gtk.Label(label="Scores from ESPN. CineHaven doesn't stream matches: live sport is licensed "
                               "to broadcasters per country, so “Where to watch” opens the official ones.",
                         wrap=True, css_classes=["dim-label", "caption"], justify=Gtk.Justification.CENTER,
                         margin_bottom=20, margin_start=16, margin_end=16)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        col.append(lc)
        col.append(foot)
        scw = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scw.set_child(col)
        self.append(scw)

    # ── visibility / refresh ─────────────────────────────────────────────
    def on_shown(self):
        self._visible = True
        self.refresh()

    def on_hidden(self):
        self._visible = False
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = None

    def show(self, key):
        self.sport = key
        self.win.settings["sport_section"] = key
        self.win.save_settings()
        while (c := self.list.get_first_child()):
            self.list.remove(c)
        self.refresh()

    def refresh(self):
        self.gen += 1
        gen, key = self.gen, self.sport
        if not self.list.get_first_child():
            self.status.set_label("Loading scores…")

        def work():
            try:
                res = sport.matches(key)
            except Exception as e:
                res = e
            GLib.idle_add(self._done, gen, res)
        threading.Thread(target=work, daemon=True).start()

    def _done(self, gen, res):
        if gen != self.gen:
            return False
        while (c := self.list.get_first_child()):
            self.list.remove(c)
        if isinstance(res, Exception):
            self.status.set_label("Couldn't reach the score service – check your internet connection.")
            res = []
        elif not res:
            self.status.set_label("No matches this round.")
        else:
            live = sum(m["state"] == "in" for m in res)
            self.status.set_label(f"{live} match{'es' if live != 1 else ''} live now · updates every "
                                  f"{LIVE_REFRESH} s" if live else "No matches live right now")
        for m in res:
            self.list.append(MatchCard(self, m))
        if self._timer:
            GLib.source_remove(self._timer)
        self._timer = None
        if self._visible:
            secs = LIVE_REFRESH if any(m["state"] == "in" for m in res) else IDLE_REFRESH
            self._timer = GLib.timeout_add_seconds(secs, self._tick)
        return False

    def _tick(self):
        self._timer = None
        if self._visible:
            self.refresh()
        return False

    # ── actions ──────────────────────────────────────────────────────────
    def open_url(self, url):
        Gtk.UriLauncher.new(url).launch(self.win, None, None)

    def highlights(self, m):
        self.win.views.set_visible_child_name("youtube")
        self.win.youtube.go(sport.highlights_query(m))

    def shutdown(self):
        self.on_hidden()

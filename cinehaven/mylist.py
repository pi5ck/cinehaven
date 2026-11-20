"""'My List' page: your wish list + where each film can be watched legally."""
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk, Pango  # noqa: E402

from . import watch  # noqa: E402

CHOICES = [("BOTH", "South Africa + UAE"), ("ZA", "South Africa"), ("AE", "UAE")]
FLAG = {"ZA": "🇿🇦", "AE": "🇦🇪"}
CHIP_CLASS = {"FREE": "success", "ADS": "success", "FLATRATE": "accent"}

CSS = b"""
.chip { padding: 3px 10px; border-radius: 99px; font-size: .85em; min-height: 0; }
.chip.success { background: alpha(@success_color,.16); color: @success_color; }
.chip.accent  { background: alpha(@accent_color,.16); color: @accent_color; }
.chip.plain   { background: alpha(currentColor,.08); }
.small-poster { border-radius: 8px; background: alpha(currentColor,.08); }
.country-lbl { font-weight: 700; font-size: .85em; }
"""


class TitleRow(Gtk.ListBoxRow):
    def __init__(self, page, t):
        super().__init__(activatable=False)
        self.page, self.t = page, t
        box = Gtk.Box(spacing=14, margin_top=10, margin_bottom=10, margin_start=12, margin_end=12)
        self.set_child(box)
        pic = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, css_classes=["small-poster"], valign=Gtk.Align.START)
        pic.set_size_request(72, 106)
        pic.set_overflow(Gtk.Overflow.HIDDEN)
        if t.get("poster"):
            page.win.load_thumb(t["poster"], pic)
        box.append(pic)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True)
        head = Gtk.Box(spacing=8)
        head.append(Gtk.Label(label=t["title"], xalign=0, wrap=True, css_classes=["heading"]))
        meta = ["TV series" if t.get("kind") == "series" else None,
                str(t["year"]) if t.get("year") else None,
                f"IMDb {t['imdb']:.1f}" if t.get("imdb") else None]
        head.append(Gtk.Label(label=" · ".join(m for m in meta if m), css_classes=["dim-label", "caption"]))
        col.append(head)
        self.offers_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.offers_box.append(Gtk.Label(label="Checking where to watch…", xalign=0,
                                         css_classes=["dim-label", "caption"]))
        col.append(self.offers_box)
        box.append(col)
        rm = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Remove from My List",
                        valign=Gtk.Align.START, css_classes=["flat", "circular"])
        rm.connect("clicked", lambda *_: page.remove(t))
        box.append(rm)

    def show_offers(self, per_country):
        """per_country: list of (country, offers | Exception)."""
        while (c := self.offers_box.get_first_child()):
            self.offers_box.remove(c)
        for country, offers in per_country:
            line = Gtk.Box(spacing=6)
            line.append(Gtk.Label(label=f"{FLAG[country]} {watch.COUNTRIES[country]}",
                                  css_classes=["country-lbl"], width_chars=14, xalign=0, valign=Gtk.Align.START,
                                  margin_top=3))
            flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, hexpand=True,
                               max_children_per_line=6, column_spacing=6, row_spacing=4)
            if isinstance(offers, Exception):
                flow.append(Gtk.Label(label="Couldn't check (offline?)", css_classes=["dim-label", "caption"]))
            elif not offers:
                q = urllib.parse.quote(self.t["title"])
                b = Gtk.Button(label="Not streaming here now · search JustWatch ↗", css_classes=["flat", "chip", "plain"])
                b.connect("clicked", lambda *_, u=f"https://www.justwatch.com/{country.lower()}/search?q={q}":
                          self.page.open_url(u))
                flow.append(b)
            else:
                rent = [o for o in offers if o["kind"] == "RENT"]
                shown = [o for o in offers if o["kind"] not in ("RENT", "BUY")] + rent
                if not shown:
                    shown = [o for o in offers if o["kind"] == "BUY"][:2]
                for o in shown[:6]:
                    text = f"{o['service']} · {o['label']}"
                    if o["kind"] in ("RENT", "BUY") and o.get("price"):
                        text += f" {o['price']}"
                    b = Gtk.Button(label=text + " ↗", tooltip_text=f"Open {o['service']}",
                                   css_classes=["flat", "chip", CHIP_CLASS.get(o["kind"], "plain")])
                    b.connect("clicked", lambda *_, u=o["url"]: self.page.open_url(u))
                    flow.append(b)
            line.append(flow)
            self.offers_box.append(line)


class ResultCard(Gtk.Box):
    def __init__(self, page, t):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4, css_classes=["movie-card"],
                         halign=Gtk.Align.CENTER, valign=Gtk.Align.START)
        self.set_size_request(130, -1)
        pic = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, css_classes=["poster"])
        pic.set_size_request(120, 176)
        pic.set_overflow(Gtk.Overflow.HIDDEN)
        if t.get("poster"):
            page.win.load_thumb(t["poster"], pic)
        self.append(pic)
        self.append(Gtk.Label(label=t["title"], wrap=True, lines=2, ellipsize=Pango.EllipsizeMode.END,
                              xalign=0, max_width_chars=14, css_classes=["caption-heading"]))
        self.append(Gtk.Label(label=("Series · " if t.get("kind") == "series" else "") + str(t.get("year") or ""),
                              xalign=0, css_classes=["dim-label", "caption"]))
        have = page.has(t)
        b = Gtk.Button(label="✔ In My List" if have else "+ My List", sensitive=not have,
                       css_classes=["pill", "suggested-action"] if not have else ["pill"])
        b.connect("clicked", lambda btn: (page.add(t), btn.set_label("✔ In My List"), btn.set_sensitive(False),
                                          btn.remove_css_class("suggested-action")))
        self.append(b)


class MyListPage(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.settings = win.settings
        self.pool = ThreadPoolExecutor(max_workers=2)
        self.rows = {}
        self.gen = 0

        top = Gtk.Box(spacing=8)
        self.entry = Gtk.SearchEntry(placeholder_text="Find any movie or series – see where to watch it legally…",
                                     hexpand=True, css_classes=["big-search"], search_delay=500)
        self.entry.connect("search-changed", lambda e: self.search(e.get_text()))
        top.append(self.entry)
        self.country = Gtk.DropDown.new_from_strings([c[1] for c in CHOICES])
        cur = self.settings.get("country", "BOTH")
        self.country.set_selected(next((i for i, c in enumerate(CHOICES) if c[0] == cur), 0))
        self.country.set_valign(Gtk.Align.CENTER)
        self.country.connect("notify::selected", self._country_changed)
        top.append(self.country)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Re-check availability",
                             valign=Gtk.Align.CENTER, css_classes=["flat", "circular"])
        refresh.connect("clicked", lambda *_: self.refresh_all(force=True))
        top.append(refresh)
        clamp = Adw.Clamp(maximum_size=980, margin_top=18, margin_start=16, margin_end=16, margin_bottom=6)
        clamp.set_child(top)
        self.append(clamp)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.results_lbl = Gtk.Label(xalign=0, css_classes=["heading"], visible=False)
        self.results = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=10,
                                   min_children_per_line=2, column_spacing=4, row_spacing=6, visible=False,
                                   homogeneous=True, valign=Gtk.Align.START)
        content.append(self.results_lbl)
        content.append(self.results)
        self.list_lbl = Gtk.Label(xalign=0, css_classes=["heading"])
        content.append(self.list_lbl)
        content.append(Gtk.Label(label="Green = free · Blue = included in a subscription · grey = rent/buy. "
                                       "Click a service to open it.", xalign=0, wrap=True,
                                 css_classes=["dim-label", "caption"]))
        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        content.append(self.listbox)
        c2 = Adw.Clamp(maximum_size=980, margin_start=16, margin_end=16, margin_bottom=24, margin_top=8)
        c2.set_child(content)
        sc = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        sc.set_child(c2)
        self.append(sc)

        self.items = self.settings.get("my_list")
        if self.items is None:
            self.items = []
            self.settings["my_list"] = self.items
            self._seed()
        for t in self.items:
            self._add_row(t)
        self._update_label()
        self.refresh_all()

    # ── list management ───────────────────────────────────────────────────
    def countries(self):
        sel = CHOICES[self.country.get_selected()][0]
        return ["ZA", "AE"] if sel == "BOTH" else [sel]

    def _country_changed(self, *_):
        self.settings["country"] = CHOICES[self.country.get_selected()][0]
        self.win.save_settings()
        self.refresh_all()

    def has(self, t):
        return any(x["id"] == t["id"] for x in self.items)

    def add(self, t):
        if self.has(t):
            return
        keep = {k: t.get(k) for k in ("id", "title", "year", "poster", "imdb", "genres", "kind")}
        self.items.insert(0, keep)
        self.win.save_settings()
        row = self._add_row(keep, top=True)
        self._update_label()
        self._load(row)

    def remove(self, t):
        self.items[:] = [x for x in self.items if x["id"] != t["id"]]
        self.win.save_settings()
        row = self.rows.pop(t["id"], None)
        if row:
            self.listbox.remove(row)
        self._update_label()

    def _add_row(self, t, top=False):
        row = TitleRow(self, t)
        self.rows[t["id"]] = row
        self.listbox.prepend(row) if top else self.listbox.append(row)
        return row

    def _update_label(self):
        n = len(self.items)
        self.list_lbl.set_label(f"My List · {n} film{'s' if n != 1 else ''}" if n else
                                "My List is empty – search above to add films")

    def _seed(self):
        """First run: resolve the starter list in the background."""
        def work():
            found = []
            for title, year in watch.STARTER_LIST:
                try:
                    t = watch.find(title, year)
                except Exception:
                    t = None
                if t:
                    found.append(t)
            GLib.idle_add(done, found)

        def done(found):
            for t in found:
                if not self.has(t):
                    keep = {k: t.get(k) for k in ("id", "title", "year", "poster", "imdb", "genres")}
                    self.items.append(keep)
                    self._load(self._add_row(keep))
            self.win.save_settings()
            self._update_label()
        self.list_lbl.set_label("Setting up your list…")
        threading.Thread(target=work, daemon=True).start()

    def refresh_all(self, force=False):
        for row in self.rows.values():
            self._load(row, force)

    def _load(self, row, force=False):
        countries = self.countries()

        def work():
            res = []
            for c in countries:
                try:
                    res.append((c, watch.offers(row.t["id"], c, force=force)))
                except Exception as e:
                    res.append((c, e))
            GLib.idle_add(row.show_offers, res)
        self.pool.submit(work)

    # ── search ────────────────────────────────────────────────────────────
    def search(self, text):
        self.gen += 1
        gen = self.gen
        text = text.strip()
        if not text:
            self.results.set_visible(False)
            self.results_lbl.set_visible(False)
            return
        self.results_lbl.set_label("Searching…")
        self.results_lbl.set_visible(True)

        def work():
            try:
                res = watch.search(text, self.countries()[0])
            except Exception as e:
                res = e
            GLib.idle_add(show, res)

        def show(res):
            if gen != self.gen:
                return
            self.results.remove_all()
            if isinstance(res, Exception):
                self.results_lbl.set_label("Couldn't search – check your connection")
                return
            self.results_lbl.set_label(f"Results for “{text}”" if res else "No movies found")
            for t in res:
                self.results.append(ResultCard(self, t))
            self.results.set_visible(bool(res))
        threading.Thread(target=work, daemon=True).start()

    def open_url(self, url):
        Gtk.UriLauncher.new(url).launch(self.win, None, None)


class StreamCard(Gtk.Box):
    """A documentary/film on a streaming service: poster, title, service buttons."""

    def __init__(self, page, t):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4, css_classes=["movie-card"],
                         valign=Gtk.Align.START)
        self.set_size_request(140, -1)
        pic = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, css_classes=["poster"])
        pic.set_size_request(130, 190)
        pic.set_overflow(Gtk.Overflow.HIDDEN)
        if t.get("poster"):
            page.win.load_thumb(t["poster"], pic)
        self.append(pic)
        self.append(Gtk.Label(label=t["title"], wrap=True, lines=2, ellipsize=Pango.EllipsizeMode.END,
                              xalign=0, max_width_chars=15, css_classes=["caption-heading"]))
        meta = ("Series" if t.get("kind") == "series" else "Film") + (f" · {t['year']}" if t.get("year") else "")
        self.append(Gtk.Label(label=meta, xalign=0, css_classes=["dim-label", "caption"]))
        for svc, (url, flags) in list(t["services"].items())[:3]:
            b = Gtk.Button(label=f"{svc} {flags} ↗", css_classes=["flat", "chip", "accent"], tooltip_text=f"Open {svc}")
            b.get_child().set_ellipsize(Pango.EllipsizeMode.END)
            b.connect("clicked", lambda *_, u=url: page.open_url(u))
            self.append(b)
        have = page.has(t)
        add = Gtk.Button(label="✔ In My List" if have else "+ My List", sensitive=not have, css_classes=["flat"])
        add.connect("clicked", lambda btn: (page.add(t), btn.set_label("✔ In My List"), btn.set_sensitive(False)))
        self.append(add)


def streaming_docs(countries):
    """Merge documentary lists for several countries: services → (url, flags)."""
    merged = {}
    for c in countries:
        for t in watch.documentaries(c, 40):
            m = merged.setdefault(t["id"], dict(t, services={}))
            for svc, url in t["services"].items():
                u, flags = m["services"].get(svc, (url, ""))
                m["services"][svc] = (u, flags + FLAG[c])
    return list(merged.values())

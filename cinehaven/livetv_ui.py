"""Live TV page."""
import threading

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk, Pango  # noqa: E402

from . import livetv  # noqa: E402

CSS = b"""
.channel-card { padding: 8px; border-radius: 14px; background: alpha(currentColor,.05); }
.channel-card:hover { background: alpha(currentColor,.10); }
.channel-logo { min-height: 72px; }
.live-dot { color: #e5383b; font-weight: 800; font-size: .8em; }
"""


class _Logo(Gtk.Image):
    """Fixed-size logo (Gtk.Picture would grow to the image's natural size)."""

    def __init__(self):
        super().__init__(pixel_size=88, icon_name="video-display-symbolic", css_classes=["channel-logo"])

    def set_paintable(self, tex):
        self.set_from_paintable(tex)


class ChannelCard(Gtk.Box):
    def __init__(self, win, ch):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["channel-card"],
                         halign=Gtk.Align.FILL, valign=Gtk.Align.START)
        self.ch = ch
        self.set_size_request(150, -1)
        pic = _Logo()
        if ch.get("logo"):
            win.load_thumb(ch["logo"], pic)
        self.append(pic)
        self.append(Gtk.Label(label=ch["name"], wrap=True, lines=2, ellipsize=Pango.EllipsizeMode.END,
                              justify=Gtk.Justification.CENTER, max_width_chars=16, css_classes=["heading"]))
        cat = ", ".join(c.title() for c in ch["cats"][:2]) or "General"
        row = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
        row.append(Gtk.Label(label="● LIVE", css_classes=["live-dot"]))
        row.append(Gtk.Label(label=f"{livetv.FLAGS.get(ch['country'], ch['country'])} {cat}",
                             css_classes=["dim-label", "caption"]))
        self.append(row)


class LiveTVPage(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.index = None
        self.health = livetv.Health()
        self.section = win.settings.get("live_section", "ZA")
        self.gen = 0
        self.on_air = []

        top = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        chips = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
        group = None
        for key, label, _f in livetv.SECTIONS:
            b = Gtk.ToggleButton(label=label, group=group, active=(key == self.section), css_classes=["pill"])
            group = group or b
            b.connect("toggled", lambda btn, k=key: btn.get_active() and self.show_section(k))
            chips.append(b)
        sc = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True)
        sc.set_child(chips)
        top.append(sc)
        self.filter = Gtk.SearchEntry(placeholder_text="Filter channels by name…")
        self.filter.connect("search-changed", lambda *_: self.flow.invalidate_filter())
        c = Adw.Clamp(maximum_size=520)
        c.set_child(self.filter)
        top.append(c)
        self.status = Gtk.Label(css_classes=["dim-label"])
        top.append(self.status)
        clamp = Adw.Clamp(maximum_size=1100, margin_top=16, margin_start=16, margin_end=16, margin_bottom=6)
        clamp.set_child(top)
        self.append(clamp)

        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True, valign=Gtk.Align.START,
                                max_children_per_line=10, min_children_per_line=2, column_spacing=10,
                                row_spacing=10, margin_start=16, margin_end=16, margin_bottom=24,
                                activate_on_single_click=True)
        self.flow.set_sort_func(lambda a, b: (a.get_child().ch["name"].lower() > b.get_child().ch["name"].lower())
                                - (a.get_child().ch["name"].lower() < b.get_child().ch["name"].lower()))
        self.flow.set_filter_func(self._filter)
        self.flow.connect("child-activated", lambda fb, child: self.play(child.get_child().ch))
        scw = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scw.set_child(self.flow)
        self.append(scw)
        self._loaded = False

    def on_shown(self):
        if not self._loaded:
            self._loaded = True
            self.status.set_label("Loading the free channel directory…")

            def work():
                try:
                    idx = livetv.load_index()
                except Exception as e:
                    idx = e
                GLib.idle_add(self._index_ready, idx)
            threading.Thread(target=work, daemon=True).start()

    def _index_ready(self, idx):
        if isinstance(idx, Exception):
            self.status.set_label("Couldn't load the channel list – check your internet connection.")
            self._loaded = False
            return
        self.index = idx
        self.show_section(self.section)

    def _filter(self, child):
        q = self.filter.get_text().strip().lower()
        return not q or q in child.get_child().ch["name"].lower()

    def show_section(self, key):
        self.section = key
        self.win.settings["live_section"] = key
        if self.index is None:
            return
        self.gen += 1
        gen = self.gen
        self.flow.remove_all()
        self.on_air = []
        f = next(f for k, _l, f in livetv.SECTIONS if k == key)
        cands = [c for c in self.index if f(c)]
        pending = []
        for ch in cands:
            h = self.health.known(ch)
            if h is None:
                pending.append(ch)
            elif h["ok"]:
                self._add(ch, h)
        self._todo = len(pending)
        self._update_status(gen)
        for ch in pending:
            self.health.check(ch, lambda c, r, g=gen: GLib.idle_add(self._checked, g, c, r))

    def _checked(self, gen, ch, res):
        if gen != self.gen:
            return False
        self._todo -= 1
        if res["ok"]:
            self._add(ch, res)
        self._update_status(gen)
        if self._todo == 0:
            self.health.save()
        return False

    def _add(self, ch, h):
        ch = dict(ch, _stream=h.get("stream", 0))
        self.on_air.append(ch)
        self.flow.append(ChannelCard(self.win, ch))

    def _update_status(self, gen):
        n = len(self.on_air)
        if self._todo:
            self.status.set_label(f"Checking which channels are on air… {n} found so far")
        else:
            self.status.set_label(f"{n} channels on air now" if n else "No channels on air in this section right now")

    def mark_offline(self, ch):
        """Called by the playback doctor: hide a channel that turned out to be off air."""
        import time
        self.health.db[ch["id"]] = {"t": time.time(), "ok": False}
        self.health.save()
        child = self.flow.get_first_child()
        while child:
            if child.get_child().ch["id"] == ch["id"]:
                self.flow.remove(child)
                break
            child = child.get_next_sibling()
        self.on_air = [c for c in self.on_air if c["id"] != ch["id"]]

    def play(self, ch):
        self.win.play_live(ch, ch["streams"][ch.get("_stream", 0)])

    def shutdown(self):
        self.health.shutdown()

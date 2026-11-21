"""More picture-in-picture: up to three extra films playing in small tiles beside the main player.

When the main player is in its corner and you start something else, the film that was playing
moves into a tile of its own and carries on from the same moment. Only one thing has sound
at a time; click 🔊 on a tile to listen to it. Double-click a tile (or ⤢) to make it the main
film – the main one then takes its place as a tile.
"""

from gi.repository import GLib, Gtk, Pango

from .player import VideoView

MAX_TILES = 3          # + the main player = 4 pictures at once


class PipTile(Gtk.Overlay):
    def __init__(self, shelf, src):
        super().__init__(css_classes=["pip-tile"], overflow=Gtk.Overflow.HIDDEN)
        self.shelf, self.src = shelf, src
        self.video = VideoView()
        m = self.video.mpv
        m.mute = True
        for prop in ("user_agent", "referrer", "http_header_fields"):
            if src.get(prop):
                try:
                    setattr(m, prop, src[prop])
                except Exception:
                    pass
        self.set_child(self.video)

        self.prog = Gtk.ProgressBar(valign=Gtk.Align.END, css_classes=["mini-prog"])
        self.add_overlay(self.prog)
        bar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["mini-bar"])
        bar.append(Gtk.Label(label=src["title"], xalign=0, ellipsize=Pango.EllipsizeMode.END,
                             css_classes=["caption-heading"], margin_start=8, margin_end=8, margin_top=6))
        mid = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, vexpand=True)
        self.pp = Gtk.Button(icon_name="media-playback-pause-symbolic", css_classes=["circular", "osd"],
                             tooltip_text="Play/Pause")
        self.pp.connect("clicked", lambda *_: setattr(m, "pause", not m.pause))
        mid.append(self.pp)
        bar.append(mid)
        bot = Gtk.Box(spacing=4, margin_start=4, margin_end=4, margin_bottom=4)
        self.snd = Gtk.Button(icon_name="audio-volume-muted-symbolic", css_classes=["circular", "osd"],
                              tooltip_text="Listen to this one (the others go quiet)")
        self.snd.connect("clicked", lambda *_: shelf.give_sound(self))
        bot.append(self.snd)
        bot.append(Gtk.Box(hexpand=True))
        b = Gtk.Button(icon_name="view-fullscreen-symbolic", css_classes=["circular", "osd"],
                       tooltip_text="Make this the main film (double-click)")
        b.connect("clicked", lambda *_: shelf.promote(self))
        bot.append(b)
        b = Gtk.Button(icon_name="window-close-symbolic", css_classes=["circular", "osd"], tooltip_text="Close")
        b.connect("clicked", lambda *_: shelf.close(self))
        bot.append(b)
        bar.append(bot)
        self.rev = Gtk.Revealer(child=bar, transition_type=Gtk.RevealerTransitionType.CROSSFADE)
        self.add_overlay(self.rev)

        mo = Gtk.EventControllerMotion()
        mo.connect("enter", lambda *_: self.rev.set_reveal_child(True))
        mo.connect("leave", lambda *_: self.rev.set_reveal_child(False))
        self.add_controller(mo)
        click = Gtk.GestureClick()
        click.connect("pressed", lambda _g, n, *_: n == 2 and shelf.promote(self))
        self.add_controller(click)
        self._started = False
        # mpv may only open the film once this tile's picture surface exists
        if self.video.get_realized():
            self._start()
        else:
            self.video.connect_after("realize", lambda *_: self._start())

    def _start(self):
        if self._started or not self.video.ctx:
            return
        self._started = True
        self.video.play(self.src["url"], start=self.src.get("pos") or None)
        if self.src.get("paused"):
            self.video.mpv.pause = True

    def position(self):
        try:
            return self.video.mpv.time_pos
        except Exception:
            return None

    def tick(self):
        m = self.video.mpv
        try:
            pos, dur, paused = m.time_pos, m.duration, m.pause
        except Exception:
            return
        self.pp.set_icon_name("media-playback-start-symbolic" if paused else "media-playback-pause-symbolic")
        self.prog.set_visible(bool(dur))
        if pos is not None and dur:
            self.prog.set_fraction(min(1, pos / dur))

    def set_sound(self, on):
        try:
            self.video.mpv.mute = not on
        except Exception:
            pass
        self.snd.set_icon_name("audio-volume-high-symbolic" if on else "audio-volume-muted-symbolic")

    def shutdown(self):
        self.video.shutdown()


class PipShelf(Gtk.Box):
    """The row of extra tiles, floating at the bottom right of the window."""

    def __init__(self, win):
        super().__init__(spacing=12, halign=Gtk.Align.END, valign=Gtk.Align.END, visible=False,
                         css_classes=["pip-shelf"])
        self.win = win
        self.tiles = []
        self.sound = None              # the tile with sound, or None = the main player
        GLib.timeout_add(500, self._tick)

    # ── capture the main player's film as a tile ─────────────────────────
    def park(self, src):
        if len(self.tiles) >= MAX_TILES:
            old = self.tiles[0]
            self.close(old)
            self.win.toast(f"Closed “{old.src['title']}” – four pictures at once is the most")
        t = PipTile(self, src)
        self.tiles.append(t)
        self.append(t)
        self.layout()
        return t

    def close(self, t):
        if t not in self.tiles:
            return
        self.tiles.remove(t)
        if self.sound is t:
            self.give_sound(None)
        self.remove(t)
        t.shutdown()
        self.layout()

    def promote(self, t):
        """Swap: this tile becomes the main film; whatever the main player shows becomes a tile."""
        pos = t.position() or t.src.get("pos")
        src = dict(t.src, pos=pos)
        self.close(t)
        self.win.pip_open_main(src)

    def give_sound(self, t):
        self.sound = t
        for x in self.tiles:
            x.set_sound(x is t)
        try:
            self.win.video.mpv.mute = t is not None
        except Exception:
            pass

    # ── sizes: everything fits in one row, main corner player included ───
    def layout(self):
        w = self.win
        main_corner = w.mini == "corner"
        n = len(self.tiles) + (1 if main_corner else 0)
        self.set_visible(bool(self.tiles) and w.mini != "window")
        if not self.tiles:
            return
        width = max(320, w.get_width())
        tw = int(max(200, min(520, width * 0.3)))
        if n:
            tw = int(min(tw, (width - 36 - 12 * (n - 1)) / n))
        for t in self.tiles:
            t.set_size_request(tw, tw * 9 // 16)
        if main_corner:
            w._player_w.set_size_request(tw, tw * 9 // 16)
        self.set_margin_end(18 + ((tw + 12) if main_corner else 0))
        in_full_player = w.root.get_visible_child_name() == "player" and not w.mini
        self.set_margin_bottom(130 if in_full_player else 18)

    def _tick(self):
        if self.tiles and getattr(self, "_w", None) != (self.win.get_width(), self.win.mini,
                                                         self.win.root.get_visible_child_name()):
            self._w = (self.win.get_width(), self.win.mini, self.win.root.get_visible_child_name())
            self.layout()
        for t in self.tiles:
            t.tick()
        return True

    def shutdown(self):
        for t in list(self.tiles):
            t.shutdown()
        self.tiles = []


CSS = b"""
.pip-tile { border-radius: 12px; box-shadow: 0 6px 22px rgba(0,0,0,.45); background: black; }
"""

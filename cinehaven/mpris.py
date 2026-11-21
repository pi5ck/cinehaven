"""Desktop media controls (MPRIS) on Linux.

Makes the keyboard's play/pause/next keys, Bluetooth headset buttons, and the desktop's
media panel (GNOME's top bar, KDE's media widget, the lock screen) control CineHaven, and
shows what's playing there with its title, length and picture.
"""
import sys

from gi.repository import Gio, GLib

BUS_NAME = "org.mpris.MediaPlayer2.cinehaven"
PATH = "/org/mpris/MediaPlayer2"
XML = """<node>
 <interface name="org.mpris.MediaPlayer2">
  <method name="Raise"/><method name="Quit"/>
  <property name="CanQuit" type="b" access="read"/><property name="CanRaise" type="b" access="read"/>
  <property name="HasTrackList" type="b" access="read"/><property name="Identity" type="s" access="read"/>
  <property name="DesktopEntry" type="s" access="read"/>
  <property name="SupportedUriSchemes" type="as" access="read"/>
  <property name="SupportedMimeTypes" type="as" access="read"/>
 </interface>
 <interface name="org.mpris.MediaPlayer2.Player">
  <method name="Next"/><method name="Previous"/><method name="Pause"/><method name="PlayPause"/>
  <method name="Stop"/><method name="Play"/>
  <method name="Seek"><arg direction="in" name="Offset" type="x"/></method>
  <method name="SetPosition"><arg direction="in" name="TrackId" type="o"/><arg direction="in" name="Position" type="x"/></method>
  <method name="OpenUri"><arg direction="in" name="Uri" type="s"/></method>
  <signal name="Seeked"><arg name="Position" type="x"/></signal>
  <property name="PlaybackStatus" type="s" access="read"/>
  <property name="Rate" type="d" access="readwrite"/>
  <property name="Metadata" type="a{sv}" access="read"/>
  <property name="Volume" type="d" access="readwrite"/>
  <property name="Position" type="x" access="read"/>
  <property name="MinimumRate" type="d" access="read"/><property name="MaximumRate" type="d" access="read"/>
  <property name="CanGoNext" type="b" access="read"/><property name="CanGoPrevious" type="b" access="read"/>
  <property name="CanPlay" type="b" access="read"/><property name="CanPause" type="b" access="read"/>
  <property name="CanSeek" type="b" access="read"/><property name="CanControl" type="b" access="read"/>
 </interface>
</node>"""
TRACK = "/io/github/cinehaven/track/current"


class MPRIS:
    """win must offer: mpris_state() -> dict, mpris_command(name, value)."""

    def __init__(self, win, app_id):
        self.win = win
        self.app_id = app_id
        self.conn = None
        self.state = {}
        self._owner = None
        if not sys.platform.startswith("linux"):
            return
        try:
            self.info = Gio.DBusNodeInfo.new_for_xml(XML)
            self._owner = Gio.bus_own_name(Gio.BusType.SESSION, BUS_NAME, Gio.BusNameOwnerFlags.NONE,
                                           self._on_bus, None, None)
        except Exception:
            self._owner = None

    def _on_bus(self, conn, _name):
        self.conn = conn
        for iface in self.info.interfaces:
            conn.register_object(PATH, iface, self._call, self._get, self._set)

    # -- incoming ----------------------------------------------------------------
    def _call(self, conn, sender, path, iface, method, params, inv):
        args = params.unpack()
        cmd = {"PlayPause": "toggle", "Play": "play", "Pause": "pause", "Stop": "stop", "Next": "next",
               "Previous": "previous", "Raise": "raise", "Quit": "quit"}.get(method)
        if cmd:
            self.win.mpris_command(cmd, None)
        elif method == "Seek":
            self.win.mpris_command("seek_rel", args[0] / 1e6)
        elif method == "SetPosition":
            self.win.mpris_command("seek_abs", args[1] / 1e6)
        elif method == "OpenUri":
            self.win.mpris_command("open", args[0])
        inv.return_value(None)

    def _get(self, conn, sender, path, iface, prop):
        st = self.win.mpris_state()
        if iface == "org.mpris.MediaPlayer2":
            return {"CanQuit": GLib.Variant("b", True), "CanRaise": GLib.Variant("b", True),
                    "HasTrackList": GLib.Variant("b", False), "Identity": GLib.Variant("s", "CineHaven"),
                    "DesktopEntry": GLib.Variant("s", self.app_id),
                    "SupportedUriSchemes": GLib.Variant("as", ["file", "magnet", "https"]),
                    "SupportedMimeTypes": GLib.Variant("as", ["video/mp4", "video/x-matroska", "video/webm"]),
                    }.get(prop)
        return self._player_props(st).get(prop)

    def _set(self, conn, sender, path, iface, prop, value):
        if prop == "Volume":
            self.win.mpris_command("volume", value.unpack() * 100)
        elif prop == "Rate":
            self.win.mpris_command("rate", value.unpack())
        return True

    # -- outgoing ----------------------------------------------------------------
    def _player_props(self, st):
        md = {"mpris:trackid": GLib.Variant("o", TRACK), "xesam:title": GLib.Variant("s", st.get("title") or "")}
        if st.get("length"):
            md["mpris:length"] = GLib.Variant("x", int(st["length"] * 1e6))
        if st.get("art"):
            md["mpris:artUrl"] = GLib.Variant("s", st["art"])
        if st.get("artist"):
            md["xesam:artist"] = GLib.Variant("as", [st["artist"]])
        active = bool(st.get("title"))
        return {"PlaybackStatus": GLib.Variant("s", st.get("status", "Stopped")),
                "Rate": GLib.Variant("d", st.get("rate", 1.0)),
                "Metadata": GLib.Variant("a{sv}", md),
                "Volume": GLib.Variant("d", (st.get("volume") or 0) / 100),
                "Position": GLib.Variant("x", int((st.get("position") or 0) * 1e6)),
                "MinimumRate": GLib.Variant("d", 0.25), "MaximumRate": GLib.Variant("d", 4.0),
                "CanGoNext": GLib.Variant("b", bool(st.get("can_next"))),
                "CanGoPrevious": GLib.Variant("b", active),
                "CanPlay": GLib.Variant("b", active), "CanPause": GLib.Variant("b", active),
                "CanSeek": GLib.Variant("b", active and not st.get("live")),
                "CanControl": GLib.Variant("b", True)}

    def update(self, **st):
        """Call often; only changes are announced to the desktop."""
        old, self.state = self.state, st
        if not self.conn:
            return
        keys = ("status", "title", "length", "art", "rate", "volume", "can_next", "live", "artist")
        changed = {}
        if any(old.get(k) != st.get(k) for k in keys):
            props = self._player_props(st)
            for k in ("PlaybackStatus", "Metadata", "Rate", "Volume", "CanGoNext", "CanGoPrevious",
                      "CanPlay", "CanPause", "CanSeek"):
                changed[k] = props[k]
        if changed:
            self._emit_changed(changed)
        # a jump (not normal playback) is announced as Seeked
        op, np = old.get("position"), st.get("position")
        if op is not None and np is not None and old.get("title") == st.get("title") \
                and abs(np - op) > 3 and old.get("status") == st.get("status"):
            try:
                self.conn.emit_signal(None, PATH, "org.mpris.MediaPlayer2.Player", "Seeked",
                                      GLib.Variant("(x)", (int(np * 1e6),)))
            except Exception:
                pass

    def _emit_changed(self, changed):
        try:
            self.conn.emit_signal(None, PATH, "org.freedesktop.DBus.Properties", "PropertiesChanged",
                                  GLib.Variant("(sa{sv}as)", ("org.mpris.MediaPlayer2.Player", changed, [])))
        except Exception:
            pass

    def stop(self):
        if self._owner:
            Gio.bus_unown_name(self._owner)
            self._owner = None

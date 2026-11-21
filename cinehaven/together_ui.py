"""Watch together – the part inside CineHaven: keeping everyone's player in step, and the room window."""
import glob
import os
import random
import threading
import time

from gi.repository import Adw, Gdk, GLib, Gtk, Pango

from . import together

INVITE = ("Watch with me on CineHaven! Open CineHaven → menu → Watch together, and join with the code {code}\n"
          "Don't have CineHaven? It's free: https://github.com/pi5ck/cinehaven/releases/latest")
FAR = 1.5          # seconds apart before someone is pulled back into step


def media_key(m):
    return f"{m['kind']}:{m['ref']}" if m else None


def fmt(t):
    t = int(max(0, t or 0))
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


class Together:
    def __init__(self, win):
        self.win = win
        self.room = None
        self.people = {}                 # id -> {"name", "seen"}
        self.chat = []                   # (name, text, mine)
        self.ref = None                  # the room's agreed state: key, pos, at, paused, speed
        self.last = None                 # my previous snapshot
        self.cur_key = None              # what my player is showing
        self.quiet_until = 0             # ignore my own changes made to follow the room
        self.opening = None              # media key being opened because someone else started it
        self.seen_state = 0
        self.status = None
        self.dlg = None
        self._last_fix = 0
        self.button = self._make_button()
        GLib.timeout_add(500, self._tick)
        GLib.timeout_add_seconds(240, self._ping)

    # ── the button in the player's top bar ───────────────────────────────
    def _make_button(self):
        box = Gtk.Box(spacing=4)
        box.append(Gtk.Image(icon_name="system-users-symbolic"))
        self.count_lbl = Gtk.Label(css_classes=["numeric"], visible=False)
        box.append(self.count_lbl)
        b = Gtk.Button(child=box, css_classes=["flat"], tooltip_text="Watch together with friends on other computers")
        b.connect("clicked", lambda *_: self.show())
        return b

    def _update_button(self):
        n = len(self.people) + 1 if self.room else 0
        self.count_lbl.set_visible(bool(self.room))
        self.count_lbl.set_label(str(n))
        if self.room:
            self.button.add_css_class("together-on")
        else:
            self.button.remove_css_class("together-on")

    # ── joining and leaving ───────────────────────────────────────────────
    def my_name(self):
        n = (self.win.settings.get("together_name") or "").strip()
        if not n:
            real = GLib.get_real_name() or ""
            n = real.split()[0] if real and real != "Unknown" else (GLib.get_user_name() or "Friend").capitalize()
        return n[:24]

    def join(self, code, name=None):
        self.leave(quiet=True)
        if name:
            self.win.settings["together_name"] = name.strip()[:24]
        self.win.settings["together_last"] = code
        self.win.save_settings()
        self.room = together.Room(code, self.my_name(),
                                  lambda msg: GLib.idle_add(self._on_message, msg),
                                  lambda st: GLib.idle_add(self._on_status, st),
                                  server=self.win.settings.get("together_server") or None)
        self.people, self.chat, self.ref, self.last, self.cur_key = {}, [], None, None, None
        self.room.send({"t": "hello"})
        self._update_button()
        self._refresh()
        self.win.toast(f"You're in room {code} – share the code with your friends")

    def leave(self, quiet=False):
        if not self.room:
            return
        r, self.room = self.room, None
        threading.Thread(target=r.close, daemon=True).start()
        self.people, self.ref = {}, None
        self._update_button()
        self._refresh()
        if not quiet:
            self.win.toast("You left the room")

    def shutdown(self):
        if self.room:
            self.room.close()
            self.room = None

    def _on_status(self, st):
        self.status = st
        self._refresh()
        return False

    def _ping(self):
        if self.room and self.people:
            self.room.send({"t": "here"})
        now = time.time()
        gone = [i for i, p in self.people.items() if now - p["seen"] > 620]
        for i in gone:
            self.people.pop(i, None)
        if gone:
            self._update_button()
            self._refresh()
        return True

    # ── my player → the room ──────────────────────────────────────────────
    def _snapshot(self):
        w = self.win
        media = w.together_media if w.root.get_visible_child_name() == "player" else None
        if not media or w.cast_dev or w.pending_play is not None:
            return None
        m = w.video.mpv
        try:
            pos, paused, speed = m.time_pos, bool(m.pause), float(m.speed or 1)
            waiting = bool(m.paused_for_cache) or bool(m.seeking)
        except Exception:
            return None
        if pos is None:
            return None
        return {"key": media_key(media), "media": media, "pos": pos, "paused": paused, "speed": speed,
                "waiting": waiting, "t": time.time()}

    def _send_state(self, s, why):
        self.ref = {"key": s["key"], "pos": s["pos"], "at": s["t"], "paused": s["paused"], "speed": s["speed"]}
        self.room.send({"t": "state", "why": why, "media": s["media"], "pos": round(s["pos"], 2),
                        "paused": s["paused"], "speed": s["speed"]})

    def _tick(self):
        if not self.room:
            return True
        s = self._snapshot()
        last, self.last = self.last, s
        if not s:
            if self.win.root.get_visible_child_name() != "player":
                self.cur_key = None
            return True
        now = s["t"]
        if s["key"] != self.cur_key:                         # something new started on my player
            self.cur_key = s["key"]
            if self.opening == s["key"]:
                self.opening = None
                self._follow()                                # it was a friend's choice: catch up with them
            elif not (self.ref and self.ref["key"] == s["key"]):
                self._send_state(s, "load")
            else:
                self._follow()
            return True
        if last is None or now < self.quiet_until or s["waiting"]:
            return True
        expect = last["pos"] + (0 if last["paused"] else (now - last["t"]) * last["speed"])
        if s["paused"] != last["paused"]:
            self._send_state(s, "pause" if s["paused"] else "play")
        elif abs(s["speed"] - last["speed"]) > 0.01:
            self._send_state(s, "speed")
        elif abs(s["pos"] - expect) > 2.5 and abs(s["pos"] - last["pos"]) > 2.0:
            self._send_state(s, "seek")
        else:
            r = self.ref
            if r and r["key"] == s["key"] and not s["paused"] and not r["paused"] and now - self._last_fix > 8:
                target = r["pos"] + (now - r["at"]) * r["speed"]
                if abs(s["pos"] - target) > FAR:
                    self._last_fix = now
                    self._seek_to(target)
        return True

    def _seek_to(self, t):
        self.quiet_until = time.time() + 1.5
        try:
            self.win.video.mpv.seek(max(0.0, t), "absolute", "exact")
        except Exception:
            pass

    def _follow(self):
        """Make my player match the room's agreed state."""
        r, w = self.ref, self.win
        s = self._snapshot()
        if not r or not s or r["key"] != s["key"]:
            return
        m = w.video.mpv
        self.quiet_until = time.time() + 1.5
        try:
            w._hide_resume()
        except Exception:
            pass
        try:
            if abs((m.speed or 1) - r["speed"]) > 0.01:
                m.speed = r["speed"]
            target = r["pos"] + (0 if r["paused"] else (time.time() - r["at"]) * r["speed"])
            if abs(s["pos"] - target) > 1.0:
                m.seek(max(0.0, target), "absolute", "exact")
            m.pause = r["paused"]
        except Exception:
            pass
        self._last_fix = time.time()

    # ── the room → my player ─────────────────────────────────────────────
    def _on_message(self, msg):
        if not self.room:
            return False
        who, name, t = msg.get("id"), (msg.get("name") or "Someone")[:24], msg.get("t")
        new = who not in self.people
        self.people[who] = {"name": name, "seen": time.time()}
        if t == "hello":
            self.win.toast(f"👋 {name} joined the room")
            self.room.send({"t": "here"})
            self._answer_later()
        elif t == "bye":
            self.people.pop(who, None)
            self.win.toast(f"{name} left the room")
        elif t == "here" and new:
            pass
        elif t == "chat":
            text = (msg.get("text") or "")[:500]
            self.chat.append((name, text, False))
            self.win.toast(f"💬 {name}: {text}")
        elif t == "state":
            self._on_state(msg, name)
        self._update_button()
        self._refresh()
        return False

    def _answer_later(self):
        """Someone new arrived: one person who's watching tells them where we are (random delay, no duplicates)."""
        asked = time.time()

        def answer():
            s = self._snapshot()
            if self.room and s and self.seen_state < asked:
                self._send_state(s, "welcome")
            return False
        GLib.timeout_add(int(random.uniform(0.3, 2.5) * 1000), answer)

    def _on_state(self, msg, name):
        media = msg.get("media")
        if not isinstance(media, dict) or not media.get("kind") or not media.get("ref"):
            return
        key = media_key(media)
        at = float(msg.get("at") or time.time())
        at = min(at, time.time())
        self.seen_state = time.time()
        self.ref = {"key": key, "pos": float(msg.get("pos") or 0), "at": at, "paused": bool(msg.get("paused")),
                    "speed": float(msg.get("speed") or 1)}
        cur = self.win.together_media if self.win.root.get_visible_child_name() == "player" else None
        why = msg.get("why")
        if media_key(cur) != key:
            if self.opening != key:
                self._open(media, name)
            return
        self._follow()
        osd = {"pause": f"⏸ {name} paused", "play": f"▶ {name} pressed play",
               "seek": f"{name} jumped to {fmt(self.ref['pos'])}", "speed": f"{name} set speed {self.ref['speed']:g}×",
               "load": f"{name} started this too"}.get(why)
        if osd:
            try:
                self.win.video.mpv.show_text(osd, 2500)
            except Exception:
                pass
            if self.win.mini:
                self.win.toast(osd)

    def _open(self, media, by):
        w = self.win
        kind, ref, title = media["kind"], media["ref"], media.get("title") or "a film"
        self.opening = media_key(media)
        w.toast(f"🎬 {by} started “{title}”")
        if kind == "youtube":
            w.play_youtube(ref, title)
        elif kind == "ia":
            it = next((i for i in w.engine.items.values() if i.meta.get("ia_id") == ref), None)
            w.watch_item(it) if it else w.watch_ia({"id": ref, "title": title})
        elif kind == "magnet":
            it = w.engine.items.get(ref)
            if it:
                w.watch_item(it)
            else:
                w.add_magnet(f"magnet:?xt=urn:btih:{ref}&dn={GLib.uri_escape_string(title, None, False)}")
        elif kind == "file":
            path = self._find_file(ref, media.get("size"))
            if path:
                w.play_local(path, title)
            else:
                self._ask_for_file(ref, title, by)
        else:
            self.opening = None

    def _find_file(self, name, size=None):
        homes = [os.path.expanduser(p) for p in ("~/Videos", "~/Downloads", "~/Movies", "~/Desktop", "~")]
        try:
            from . import DOWNLOAD_DIR
            homes.insert(0, DOWNLOAD_DIR)
        except Exception:
            pass
        for e in self.win.settings.get("history", []):
            if e.get("kind") == "file" and os.path.basename(e.get("ref", "")) == name and os.path.isfile(e["ref"]):
                return e["ref"]
        for d in homes:
            for pat in (name, f"*/{name}", f"*/*/{name}"):
                for p in glob.glob(os.path.join(glob.escape(d), pat)):
                    if os.path.isfile(p) and (not size or os.path.getsize(p) == size):
                        return p
        return None

    def _ask_for_file(self, name, title, by):
        w = self.win
        d = Adw.AlertDialog(heading=f"{by} is playing their own copy of “{title}”",
                            body=f"To watch together you need the same video on this computer ({name}). "
                                 "Choose your copy and CineHaven will put it in step.")
        d.add_response("no", "Not now")
        d.add_response("pick", "Choose file…")
        d.set_default_response("pick")
        d.set_response_appearance("pick", Adw.ResponseAppearance.SUGGESTED)

        def done(_d, resp):
            if resp != "pick":
                self.opening = None
                return
            fd = Gtk.FileDialog(title=f"Your copy of {name}")
            fd.open(w, None, lambda dlg, res: picked(dlg, res))

        def picked(dlg, res):
            try:
                f = dlg.open_finish(res)
                w.play_local(f.get_path(), title)
            except GLib.Error:
                self.opening = None
        d.connect("response", done)
        d.present(w)

    # ── chat ──────────────────────────────────────────────────────────────
    def say(self, text):
        text = text.strip()
        if not text or not self.room:
            return
        self.room.send({"t": "chat", "text": text[:500]})
        self.chat.append((self.my_name(), text, True))
        self._refresh()

    # ── the room window ──────────────────────────────────────────────────
    def show(self):
        if self.dlg:
            self.dlg.present(self.win)
            return
        d = Adw.Dialog(title="Watch together", content_width=460, content_height=600)
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        tv.set_content(self.body)
        d.set_child(tv)
        d.connect("closed", lambda *_: setattr(self, "dlg", None))
        self.dlg = d
        self._refresh()
        d.present(self.win)

    def _refresh(self):
        if not self.dlg:
            return
        c = self.body.get_first_child()
        while c:
            n = c.get_next_sibling()
            self.body.remove(c)
            c = n
        self.body.append(self._room_page() if self.room else self._start_page())

    def _start_page(self):
        sc = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_top=12, margin_bottom=18,
                      margin_start=18, margin_end=18)
        box.append(Gtk.Image(icon_name="system-users-symbolic", pixel_size=56, css_classes=["dim-label"]))
        box.append(Gtk.Label(label="Watch in step with friends on other computers – anywhere in the world. "
                                   "Pause, jumps and chat are shared; everyone plays their own copy.",
                             wrap=True, justify=Gtk.Justification.CENTER, css_classes=["dim-label"]))
        g = Adw.PreferencesGroup()
        name = Adw.EntryRow(title="Your name")
        name.set_text(self.my_name())
        g.add(name)
        box.append(g)
        start = Gtk.Button(label="Start a room", css_classes=["pill", "suggested-action"], halign=Gtk.Align.CENTER)
        start.connect("clicked", lambda *_: self.join(together.new_code(), name.get_text()))
        box.append(start)
        g2 = Adw.PreferencesGroup(title="Join a friend's room")
        code = Adw.EntryRow(title="Room code, like 7K4M-Q9TX-2BC")
        last = self.win.settings.get("together_last")
        err = Gtk.Label(css_classes=["error", "caption"], visible=False, xalign=0)

        def go(*_):
            c = together.clean_code(code.get_text())
            if not c:
                err.set_label("That doesn't look like a room code – it has 11 letters and numbers.")
                err.set_visible(True)
                return
            self.join(c, name.get_text())
        code.connect("entry-activated", go)
        jb = Gtk.Button(label="Join", valign=Gtk.Align.CENTER, css_classes=["suggested-action"])
        jb.connect("clicked", go)
        code.add_suffix(jb)
        g2.add(code)
        box.append(g2)
        box.append(err)
        if last:
            rb = Gtk.Button(label=f"Rejoin {last}", css_classes=["flat"], halign=Gtk.Align.CENTER)
            rb.connect("clicked", lambda *_: self.join(last, name.get_text()))
            box.append(rb)
        g3 = Adw.PreferencesGroup()
        ex = Adw.ExpanderRow(title="Works with", subtitle="Discover films, YouTube, torrents, and your own files")
        for t in ("Films from Discover, YouTube links and torrents open by themselves for everyone.",
                  "For your own video files, each person needs the same file – CineHaven looks for it, "
                  "or asks them to pick it.",
                  "Messages are end-to-end encrypted with the room code and pass through the free ntfy.sh "
                  "relay, which keeps nothing. Anyone with the code can join – share it only with friends."):
            ex.add_row(Adw.ActionRow(title=t, title_lines=0, css_classes=["caption"]))
        srv = Adw.EntryRow(title="Relay server (advanced)")
        srv.set_text(self.win.settings.get("together_server") or together.DEFAULT_SERVER)
        srv.connect("changed", lambda e: (self.win.settings.__setitem__(
            "together_server", e.get_text().strip() if e.get_text().strip() != together.DEFAULT_SERVER else None),
            self.win.save_settings()))
        ex.add_row(srv)
        g3.add(ex)
        box.append(g3)
        sc.set_child(box)
        return sc

    def _room_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=6, margin_bottom=12,
                      margin_start=14, margin_end=14, vexpand=True)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["card"], margin_bottom=4)
        card.append(Gtk.Label(label="Room code", css_classes=["caption", "dim-label"], margin_top=10))
        card.append(Gtk.Label(label=self.room.code, selectable=True, css_classes=["title-1", "numeric", "room-code"]))
        row = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER, margin_bottom=10)
        for label, text in (("Copy code", self.room.code), ("Copy invite", INVITE.format(code=self.room.code))):
            b = Gtk.Button(label=label, css_classes=["pill"])
            b.connect("clicked", lambda _b, t=text, l=label: (Gdk.Display.get_default().get_clipboard().set(t),
                                                              self.win.toast(f"{l.split()[1].capitalize()} copied")))
            row.append(b)
        card.append(row)
        box.append(card)
        if self.status:
            box.append(Gtk.Label(label=self.status, css_classes=["warning", "caption"]))
        names = [f"{self.my_name()} (you)"] + [p["name"] for p in self.people.values()]
        box.append(Gtk.Label(label=("👥 " + ", ".join(names)) if self.people else
                             "Waiting for friends… send them the code or the invite.",
                             wrap=True, xalign=0, css_classes=["dim-label"]))
        log = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        if not self.chat:
            log.append(Gtk.Label(label="Chat with the room here", css_classes=["dim-label"], margin_top=14,
                                 margin_bottom=14))
        for who, text, mine in self.chat[-80:]:
            r = Gtk.Box(spacing=8, margin_top=6, margin_bottom=6, margin_start=10, margin_end=10)
            r.append(Gtk.Label(label=who, css_classes=["heading"] + (["accent"] if mine else []), valign=Gtk.Align.START))
            r.append(Gtk.Label(label=text, wrap=True, xalign=0, hexpand=True, selectable=True,
                               wrap_mode=Pango.WrapMode.WORD_CHAR))
            log.append(r)
        sc = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        sc.set_child(log)
        GLib.idle_add(lambda: (sc.get_vadjustment().set_value(sc.get_vadjustment().get_upper()), False)[1])
        box.append(sc)
        entry = Gtk.Entry(placeholder_text="Say something…", hexpand=True)
        entry.connect("activate", lambda e: (self.say(e.get_text()), e.set_text("")))
        send = Gtk.Button(icon_name="mail-send-symbolic", css_classes=["suggested-action"], tooltip_text="Send")
        send.connect("clicked", lambda *_: (self.say(entry.get_text()), entry.set_text("")))
        er = Gtk.Box(spacing=6)
        er.append(entry)
        er.append(send)
        box.append(er)
        GLib.idle_add(lambda: (entry.grab_focus(), False)[1])
        lv = Gtk.Button(label="Leave room", css_classes=["pill", "destructive-action"], halign=Gtk.Align.CENTER,
                        margin_top=4)
        lv.connect("clicked", lambda *_: self.leave())
        box.append(lv)
        return box


CSS = b"""
.together-on { color: @accent_color; }
.room-code { letter-spacing: 3px; }
"""

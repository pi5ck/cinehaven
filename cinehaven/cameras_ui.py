"""Cameras page: a live grid of your own security cameras, DVR and NVR channels."""
import os
import threading
import time

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from . import cameras as C  # noqa: E402
from .player import VideoView  # noqa: E402

CSS = b"""
.cam-tile { border-radius: 12px; background: #000; }
.cam-name { background: alpha(black,.55); color: white; border-radius: 8px; padding: 2px 8px; font-weight: 700;
            font-size: .9em; }
.cam-state { color: white; background: alpha(black,.55); border-radius: 10px; padding: 6px 12px; }
.cam-menu { background: alpha(black,.45); color: white; border-radius: 99px; min-width: 28px; min-height: 28px; }
"""

SNAP_DIR = os.path.join(GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PICTURES) or os.path.expanduser("~"),
                        "CineHaven Cameras")
MAKERS = {"hikvision": "hikvision", "dahua": "dahua", "amcrest": "dahua", "lorex": "dahua", "reolink": "reolink",
          "uniview": "uniview", "axis": "axis", "tp-link": "tapo", "hanwha": "hanwha", "samsung": "hanwha",
          "vivotek": "vivotek", "foscam": "foscam", "annke": "hikvision", "swann": "swann"}
AUTO, PASTE = "auto", "paste"


class CameraTile(Gtk.Overlay):
    def __init__(self, page, cam):
        super().__init__(css_classes=["cam-tile"], overflow=Gtk.Overflow.HIDDEN)
        self.page, self.cam = page, cam
        self.video = VideoView()
        m = self.video.mpv
        m.mute = True
        m.cache = "no"
        m.cache_pause = False
        m.demuxer_lavf_o = "fflags=+nobuffer"
        m.demuxer_lavf_analyzeduration = 0.5
        m.video_latency_hacks = True
        frame = Gtk.AspectFrame(ratio=16 / 9, obey_child=False)
        frame.set_child(self.video)
        frame.set_size_request(320, 180)
        self.set_child(frame)
        name = Gtk.Label(label=cam["name"], css_classes=["cam-name"], halign=Gtk.Align.START,
                         valign=Gtk.Align.END, margin_start=8, margin_bottom=8)
        self.add_overlay(name)
        self.state = Gtk.Label(label="Connecting…", css_classes=["cam-state"], halign=Gtk.Align.CENTER,
                               valign=Gtk.Align.CENTER, wrap=True, max_width_chars=28)
        self.add_overlay(self.state)

        menu = Gtk.MenuButton(icon_name="view-more-symbolic", css_classes=["cam-menu", "circular", "flat"],
                              halign=Gtk.Align.END, valign=Gtk.Align.START, margin_top=6, margin_end=6,
                              tooltip_text="Camera options")
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for label, cb in (("Watch full screen", lambda: page.win.play_camera(cam)),
                          ("Save a snapshot", self.snapshot),
                          ("Edit…", lambda: page.edit(cam)),
                          ("Remove", lambda: page.remove(cam))):
            b = Gtk.Button(label=label, css_classes=["flat"])
            b.get_child().set_xalign(0)
            b.connect("clicked", lambda *_, f=cb: (pop.popdown(), f()))
            box.append(b)
        pop.set_child(box)
        menu.set_popover(pop)
        self.add_overlay(menu)

        click = Gtk.GestureClick()
        click.connect("released", lambda *_: page.win.play_camera(cam))
        frame.add_controller(click)
        self.set_cursor_from_name("pointer")
        self._started = 0
        self._timer = None

    def start(self):
        url = self.cam.get("sub") or self.cam["main"]
        self.state.set_label("Connecting…")
        self.state.set_visible(True)
        self._started = time.time()
        self.video.play(url)
        if not self._timer:
            self._timer = GLib.timeout_add(1000, self._tick)

    def stop(self):
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = None
        self.video.stop()

    def _tick(self):
        m = self.video.mpv
        try:
            has = bool(m.video_params) and m.time_pos is not None
        except Exception:
            has = False
        if has:
            self.state.set_visible(False)
            self._started = time.time()
        elif time.time() - self._started > 15:
            self.state.set_label("No picture – trying again…")
            self.state.set_visible(True)
            self.video.play(self.cam.get("sub") or self.cam["main"])
            self._started = time.time()
        return True

    def snapshot(self):
        os.makedirs(SNAP_DIR, exist_ok=True)
        path = os.path.join(SNAP_DIR, f"{self.cam['name']} {time.strftime('%Y-%m-%d %H.%M.%S')}.jpg".replace("/", "-"))
        try:
            self.video.mpv.command("screenshot-to-file", path, "video")
            self.page.win.toast(f"Snapshot saved in Pictures › CineHaven Cameras")
        except Exception as e:
            self.page.win.toast(f"Couldn't save a snapshot: {e}")

    def shutdown(self):
        self.stop()
        self.video.shutdown()


class CamerasPage(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.tiles = []
        self.shown = False

        bar = Gtk.Box(spacing=8, margin_top=12, margin_bottom=6, margin_start=16, margin_end=16)
        bar.append(Gtk.Label(label="Your cameras", css_classes=["title-3"], hexpand=True, xalign=0))
        self.search_btn = Gtk.Button(label="Search my network", css_classes=["pill"])
        self.search_btn.connect("clicked", lambda *_: self.search())
        bar.append(self.search_btn)
        add = Gtk.Button(label="Add camera", css_classes=["pill", "suggested-action"])
        add.connect("clicked", lambda *_: self.edit(None))
        bar.append(add)
        self.bar = bar

        self.stack = Gtk.Stack(vexpand=True)
        empty = Adw.StatusPage(icon_name="camera-web-symbolic", title="Watch your security cameras",
                               description="Works with IP cameras, DVRs and NVRs – Hikvision, Dahua, Reolink, "
                                           "Uniview, Axis, TP-Link Tapo, Swann, Lorex, Amcrest and any camera that "
                                           "speaks ONVIF, RTSP, MJPEG, HLS or RTMP.")
        eb = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        b1 = Gtk.Button(label="Search my network", css_classes=["pill"])
        b1.connect("clicked", lambda *_: self.search())
        b2 = Gtk.Button(label="Add camera", css_classes=["pill", "suggested-action"])
        b2.connect("clicked", lambda *_: self.edit(None))
        eb.append(b1)
        eb.append(b2)
        empty.set_child(eb)
        self.stack.add_named(empty, "empty")
        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True, min_children_per_line=1,
                                max_children_per_line=4, row_spacing=10, column_spacing=10, valign=Gtk.Align.START,
                                margin_start=16, margin_end=16, margin_bottom=16)
        sc = Gtk.ScrolledWindow(vexpand=True)
        sc.set_child(self.flow)
        self.stack.add_named(sc, "grid")
        self.append(bar)
        self.append(self.stack)
        self.reload()

    # -- showing ---------------------------------------------------------------
    def reload(self):
        for t in self.tiles:
            t.shutdown()
        self.flow.remove_all()
        self.tiles = []
        cams = C.load()
        for cam in cams:
            t = CameraTile(self, cam)
            self.tiles.append(t)
            self.flow.append(t)
        n = len(cams)
        self.flow.set_max_children_per_line(1 if n == 1 else 2 if n in (2, 4) else 3 if n <= 9 else 4)
        self.stack.set_visible_child_name("grid" if cams else "empty")
        self.bar.set_visible(bool(cams))
        if self.shown:
            for t in self.tiles:
                t.start()

    def on_shown(self):
        if self.shown:
            return
        self.shown = True
        for t in self.tiles:
            t.start()

    def on_hidden(self):
        if not self.shown:
            return
        self.shown = False
        for t in self.tiles:
            t.stop()

    def shutdown(self):
        for t in self.tiles:
            t.shutdown()

    def remove(self, cam):
        dlg = Adw.AlertDialog(heading=f"Remove “{cam['name']}”?", body="You can add it again at any time.")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("rm", "Remove")
        dlg.set_response_appearance("rm", Adw.ResponseAppearance.DESTRUCTIVE)

        def done(_d, r):
            if r == "rm":
                C.save([c for c in C.load() if c["id"] != cam["id"]])
                self.reload()
        dlg.connect("response", done)
        dlg.present(self.win)

    # -- searching ---------------------------------------------------------------
    def search(self):
        d = Adw.Dialog(title="Cameras on your network", content_width=480, content_height=460)
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        stack = Gtk.Stack()
        wait = Adw.StatusPage(title="Searching…", description="Asking cameras and recorders to answer")
        wait.set_child(Adw.Spinner() if hasattr(Adw, "Spinner") else Gtk.Spinner(spinning=True))
        stack.add_named(wait, "wait")
        tv.set_content(stack)
        d.set_child(tv)
        d.present(self.win)
        known = {c.get("host") for c in C.load()}

        def progress(text):
            GLib.idle_add(lambda: (wait.set_description(text), False)[1])

        def work():
            try:
                found = C.search(progress)
            except Exception as e:
                found, err = [], str(e)
            else:
                err = None
            GLib.idle_add(show, found, err)

        def show(found, err):
            if not found:
                page = Adw.StatusPage(icon_name="camera-web-symbolic", title="No cameras answered",
                                      description=err or "Some cameras don't announce themselves. Add yours with "
                                                         "its address – “Add camera” can work out the rest.")
                b = Gtk.Button(label="Add camera", css_classes=["pill", "suggested-action"], halign=Gtk.Align.CENTER)
                b.connect("clicked", lambda *_: (d.close(), self.edit(None)))
                page.set_child(b)
                stack.add_named(page, "none")
                stack.set_visible_child_name("none")
                return False
            pp = Adw.PreferencesPage()
            g = Adw.PreferencesGroup(title=f"Found {len(found)}",
                                     description="Pick one, then enter its user name and password.")
            for f in found:
                row = Adw.ActionRow(title=f["name"], subtitle=f["host"] + (" · ONVIF" if f.get("xaddr") else ""),
                                    activatable=True)
                if f["host"] in known:
                    row.add_suffix(Gtk.Label(label="Added", css_classes=["dim-label"]))
                row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
                row.connect("activated", lambda *_, f=f: (d.close(), self.edit(None, found=f)))
                g.add(row)
            pp.add(g)
            stack.add_named(pp, "list")
            stack.set_visible_child_name("list")
            return False
        threading.Thread(target=work, daemon=True).start()

    # -- adding and editing --------------------------------------------------------
    def edit(self, cam, found=None):
        cam = dict(cam or {})
        keys = list(C.BRANDS) + [AUTO, PASTE]
        labels = [C.BRANDS[k][0] for k in C.BRANDS] + ["I don't know – find it for me", "Paste a stream address"]
        d = Adw.Dialog(title="Edit camera" if cam.get("id") else "Add camera", content_width=520, content_height=640)
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: d.close())
        hb.pack_start(cancel)
        save = Gtk.Button(label="Save", css_classes=["suggested-action"])
        hb.pack_end(save)
        tv.add_top_bar(hb)
        pp = Adw.PreferencesPage()

        g1 = Adw.PreferencesGroup()
        name = Adw.EntryRow(title="Name", text=cam.get("name") or (found or {}).get("name", "").split(" · ")[0]
                            or "")
        make = Adw.ComboRow(title="Make", model=Gtk.StringList.new(labels))
        brand = cam.get("brand") or (AUTO if found else AUTO)
        make.set_selected(keys.index(brand) if brand in keys else keys.index(AUTO))
        g1.add(name)
        g1.add(make)
        pp.add(g1)

        g2 = Adw.PreferencesGroup(title="Where")
        host = Adw.EntryRow(title="Address (IP or host name)", text=cam.get("host") or (found or {}).get("host", ""))
        port = Adw.SpinRow.new_with_range(0, 65535, 1)
        port.set_title("Port")
        port.set_subtitle("0 = the usual one for this make")
        port.set_value(cam.get("port") or (found or {}).get("port") or 0)
        chans = Adw.SpinRow.new_with_range(1, 64, 1)
        chans.set_title("Cameras on this recorder")
        chans.set_subtitle("For a DVR or NVR – adds one tile per channel")
        chans.set_value(cam.get("channels") or 1)
        first = Adw.SpinRow.new_with_range(1, 64, 1)
        first.set_title("Channel")
        first.set_value(cam.get("channel") or 1)
        for w in (host, port, chans, first):
            g2.add(w)
        pp.add(g2)

        g3 = Adw.PreferencesGroup(title="Stream address",
                                  description="rtsp://, rtsps://, http:// (MJPEG), https://….m3u8 (HLS), rtmp://, "
                                              "srt:// – from the camera's manual or app (UniFi Protect: "
                                              "Settings › Advanced › RTSP)")
        url = Adw.EntryRow(title="Main stream", text=cam.get("url") or "")
        suburl = Adw.EntryRow(title="Smaller stream for the grid (optional)", text=cam.get("suburl") or "")
        g3.add(url)
        g3.add(suburl)
        pp.add(g3)

        g4 = Adw.PreferencesGroup(title="Login")
        user = Adw.EntryRow(title="User name", text=cam.get("user") or ("" if cam.get("id") else "admin"))
        pw = Adw.PasswordEntryRow(title="Password", text=cam.get("password") or "")
        g4.add(user)
        g4.add(pw)
        pp.add(g4)

        status = Gtk.Label(wrap=True, margin_top=4, margin_start=12, margin_end=12, justify=Gtk.Justification.CENTER)
        gs = Adw.PreferencesGroup()
        gs.add(status)
        pp.add(gs)

        def kind():
            return keys[make.get_selected()]

        def sync(*_):
            k = kind()
            g3.set_visible(k == PASTE)
            g2.set_visible(k != PASTE)
            nvr = k not in (PASTE, "tapo", "foscam", "eufy", "wyze", "ipwebcam", "mjpeg", "vivotek", "hanwha")
            chans.set_visible(nvr and not cam.get("id"))
            first.set_visible(nvr and bool(cam.get("id")))
            save.set_label("Find and save" if k == AUTO else "Save")
        make.connect("notify::selected", sync)
        sync()
        busy = {"on": False, "anyway": False}

        def fields():
            return dict(name=name.get_text().strip(), brand=kind(), host=host.get_text().strip(),
                        port=int(port.get_value()), channels=int(chans.get_value()), channel=int(first.get_value()),
                        user=user.get_text().strip(), password=pw.get_text(), url=url.get_text().strip(),
                        suburl=suburl.get_text().strip())

        def work(f, anyway):
            try:
                made = self._make(f, cam, anyway, lambda t: GLib.idle_add(lambda: (status.set_label(t), False)[1]))
            except C.AuthError as e:
                GLib.idle_add(fail, f"Wrong login: {e}.", False)
                return
            except Exception as e:
                GLib.idle_add(fail, f"Couldn't play it: {e}.", f["brand"] != AUTO)
                return
            GLib.idle_add(done, made)

        def fail(text, allow_anyway):
            busy["on"] = False
            save.set_sensitive(True)
            status.set_label(text)
            status.set_css_classes(["error"])
            if allow_anyway:
                busy["anyway"] = True
                save.set_label("Save anyway")
            return False

        def done(made):
            cams = [c for c in C.load() if c["id"] != cam.get("id")]
            if cam.get("id"):
                idx = next((i for i, c in enumerate(C.load()) if c["id"] == cam["id"]), len(cams))
                cams[idx:idx] = made
            else:
                cams += made
            C.save(cams)
            d.close()
            self.reload()
            self.win.toast(f"Added {len(made)} cameras" if len(made) > 1 else f"Saved “{made[0]['name']}”")
            return False

        def go(*_):
            if busy["on"]:
                return
            f = fields()
            if f["brand"] == PASTE and not C.looks_like_stream(f["url"]):
                status.set_label("Paste an address starting with rtsp://, http://, https://, rtmp:// or srt://")
                return
            if f["brand"] != PASTE and not f["host"]:
                status.set_label("Type the camera's or recorder's address, e.g. 192.168.1.64")
                return
            busy["on"] = True
            save.set_sensitive(False)
            status.set_css_classes([])
            status.set_label("Connecting…")
            threading.Thread(target=work, args=(f, busy["anyway"]), daemon=True).start()
        save.connect("clicked", go)
        tv.set_content(pp)
        d.set_child(tv)
        d.present(self.win)

    def _make(self, f, old, anyway, progress):
        """Turn the form into saved camera records (one per recorder channel), testing the first."""
        base_name = f["name"] or "Camera"
        recs = []
        if f["brand"] == PASTE:
            main = C.with_auth(f["url"], f["user"], f["password"])
            sub = C.with_auth(f["suburl"], f["user"], f["password"]) if f["suburl"] else None
            if not anyway:
                progress("Testing the stream…")
                info = C.test(main)
                progress(f"✓ {C.describe(info)}")
            return [{**old, **f, "id": old.get("id") or C.new_id(), "name": base_name, "main": main, "sub": sub}]
        brand = f["brand"]
        if brand == AUTO:
            b, main, sub, info = C.find_stream(f["host"], f["user"], f["password"], f["channel"],
                                               f["port"] or None, progress)
            progress(f"✓ {C.describe(info)}")
            if b == "onvif":
                maker = next((v for k, v in MAKERS.items() if k in (info.get("maker") or "").lower()), None)
                if f["channels"] > 1 and maker:
                    brand = maker
                else:
                    return [{**old, **f, "brand": PASTE, "url": main, "suburl": sub or "",
                             "id": old.get("id") or C.new_id(), "name": base_name, "main": main, "sub": sub}]
            else:
                brand = b
            port = int(main.split("@")[-1].split("/")[0].rsplit(":", 1)[1]) if ":" in main.split("@")[-1].split("/")[0] else 0
            f = dict(f, brand=brand, port=f["port"] or port)
        chans = [f["channel"]] if old.get("id") else list(range(1, f["channels"] + 1))
        for ch in chans:
            main, sub = C.build(brand, f["host"], f["port"] or None, f["user"], f["password"], ch)
            if ch == chans[0] and not anyway and f["brand"] != AUTO:
                progress("Testing the stream…")
                C.test(main)
            name = base_name if len(chans) == 1 else f"{base_name} {ch}"
            recs.append(dict(f, id=(old.get("id") if old.get("id") else C.new_id()), name=name, channel=ch,
                             channels=1, main=main, sub=sub))
        return recs

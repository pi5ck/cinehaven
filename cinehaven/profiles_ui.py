"""Profiles and kids mode – the parts you see: the avatar button, "Who's watching?",
managing profiles, the grown-ups' PIN, and the screen-time limit."""
import time

from gi.repository import Adw, GLib, Gtk

from . import profiles

# pages and actions a kids profile can't reach
KID_HIDDEN_PAGES = ("videos", "cameras", "youtube", "mylist", "library")
KID_BLOCKED_ACTIONS = ("open-magnet", "open-torrent", "open-video", "open-youtube", "together", "storage",
                       "open-downloads", "phone-remote", "video-tech", "english-only", "show-unverified",
                       "check-updates", "diagnostics")


class ProfilesUI:
    def __init__(self, win):
        self.win = win
        self.settings = win.settings
        if profiles.ensure(self.settings, profiles.default_name()):
            win.save_settings()
        self.button = Gtk.MenuButton(css_classes=["flat", "avatar-btn"], tooltip_text="Who's watching")
        self.button.set_create_popup_func(self._popover)
        self._last_tick = None
        self._saved_at = 0

    # ── header avatar ────────────────────────────────────────────────────
    def _update_button(self):
        p = profiles.get(self.settings)
        self.button.set_label(f"{p['emoji']}  {p['name']}")

    def _popover(self, mb):
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6, margin_bottom=6,
                      margin_start=6, margin_end=6)
        cur = self.settings.get("profile")
        for p in profiles.all_(self.settings):
            b = Gtk.Button(css_classes=["flat"])
            row = Gtk.Box(spacing=10)
            row.append(Gtk.Label(label=p["emoji"], css_classes=["title-3"]))
            row.append(Gtk.Label(label=p["name"] + ("  · kids" if p.get("kid") else ""), xalign=0, hexpand=True))
            if p["id"] == cur:
                row.append(Gtk.Image(icon_name="object-select-symbolic"))
            b.set_child(row)
            b.connect("clicked", lambda _b, pid=p["id"]: (pop.popdown(), self.choose(pid)))
            box.append(b)
        box.append(Gtk.Separator(margin_top=4, margin_bottom=4))
        m = Gtk.Button(label="Manage profiles…", css_classes=["flat"])
        m.get_child().set_xalign(0)
        m.connect("clicked", lambda *_: (pop.popdown(), self.guarded(self.manage)))
        box.append(m)
        pop.set_child(box)
        mb.set_popover(pop)

    # ── switching ────────────────────────────────────────────────────────
    def choose(self, pid):
        """Kids can go to another kids profile freely; anything else needs the PIN when leaving kids mode."""
        if pid == self.settings.get("profile"):
            return
        target = profiles.get(self.settings, pid)
        if profiles.is_kid(self.settings) and not target.get("kid"):
            self.ask_pin("Leaving kids mode", "A grown-up's PIN, please", lambda: self._switch(pid))
        else:
            self._switch(pid)

    def _switch(self, pid):
        w = self.win
        if w.root.get_visible_child_name() == "player":
            w.leave_player()
        w._remember_position() if hasattr(w, "_remember_position") else None
        profiles.switch(self.settings, pid)
        w.save_settings()
        self.apply()
        p = profiles.get(self.settings)
        w.toast(f"Hi {p['name']} {p['emoji']}")

    def guarded(self, fn):
        if profiles.is_kid(self.settings) or (profiles.has_pin(self.settings) and self._kids_exist()):
            self.ask_pin("Grown-ups only", "Enter the PIN to manage profiles", fn)
        else:
            fn()

    def _kids_exist(self):
        return any(p.get("kid") for p in profiles.all_(self.settings))

    # ── who's watching? ──────────────────────────────────────────────────
    def maybe_ask_at_start(self):
        ps = profiles.all_(self.settings)
        if len(ps) < 2 or profiles.is_kid(self.settings):
            return                                   # kids mode stays on until a grown-up leaves it
        d = Adw.Dialog(title="Who's watching?", content_width=640, can_close=False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24, margin_top=36, margin_bottom=36,
                      margin_start=24, margin_end=24)
        box.append(Gtk.Label(label="Who's watching?", css_classes=["title-1"]))
        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=5, homogeneous=True,
                           halign=Gtk.Align.CENTER, column_spacing=18, row_spacing=18)
        for p in ps:
            b = Gtk.Button(css_classes=["flat", "who-btn"])
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            face = Gtk.Label(label=p["emoji"], css_classes=["who-face"])
            face.set_size_request(104, 104)
            col.append(face)
            col.append(Gtk.Label(label=p["name"], css_classes=["heading"]))
            if p.get("kid"):
                col.append(Gtk.Label(label="Kids", css_classes=["caption", "kids-pill"], halign=Gtk.Align.CENTER))
            b.set_child(col)
            b.connect("clicked", lambda _b, pid=p["id"]: (d.set_can_close(True), d.close(), self._switch(pid)
                                                         if pid != self.settings.get("profile") else None))
            flow.append(b)
        box.append(flow)
        d.set_child(box)
        d.present(self.win)

    # ── the grown-ups' PIN ───────────────────────────────────────────────
    def ask_pin(self, heading, body, on_ok):
        if not profiles.has_pin(self.settings):
            on_ok()
            return
        a = Adw.AlertDialog(heading=heading, body=body)
        e = Gtk.PasswordEntry(show_peek_icon=True, activates_default=True, placeholder_text="PIN")
        a.set_extra_child(e)
        a.add_response("cancel", "Cancel")
        a.add_response("ok", "OK")
        a.set_default_response("ok")
        a.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)

        def resp(_a, r):
            if r != "ok":
                return
            ok = profiles.check_pin(self.settings, e.get_text())
            if ok:
                on_ok()
            elif ok is None:
                self.win.toast("Too many tries – wait a minute")
            else:
                self.win.toast("That PIN isn't right")
        a.connect("response", resp)
        a.present(self.win)
        GLib.idle_add(lambda: (e.grab_focus(), False)[1])

    def set_pin_dialog(self, then=None):
        a = Adw.AlertDialog(heading="Choose a grown-ups' PIN",
                            body="Kids profiles need this PIN to leave kids mode, change profiles or add "
                                 "screen time. 4 to 8 digits.")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        e1 = Gtk.PasswordEntry(show_peek_icon=True, placeholder_text="New PIN")
        e2 = Gtk.PasswordEntry(show_peek_icon=True, placeholder_text="The same PIN again", activates_default=True)
        box.append(e1)
        box.append(e2)
        a.set_extra_child(box)
        a.add_response("cancel", "Cancel")
        a.add_response("ok", "Save PIN")
        a.set_default_response("ok")
        a.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)

        def resp(_a, r):
            if r != "ok":
                return
            p1, p2 = e1.get_text(), e2.get_text()
            if not profiles.pin_ok_format(p1):
                self.win.toast("A PIN is 4 to 8 digits")
                return
            if p1 != p2:
                self.win.toast("The two PINs don't match")
                return
            profiles.set_pin(self.settings, p1)
            self.win.save_settings()
            self.win.toast("PIN saved")
            if then:
                then()
        a.connect("response", resp)
        a.present(self.win)

    # ── managing profiles ────────────────────────────────────────────────
    def manage(self):
        d = Adw.Dialog(title="Profiles", content_width=480, content_height=560)
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()
        g = Adw.PreferencesGroup(description="Each profile has its own Recently played and resume points. "
                                             "Kids profiles only see family films and children's channels.")
        cur = self.settings.get("profile")
        for p in profiles.all_(self.settings):
            sub = ["Kids"] if p.get("kid") else ["Grown-up"]
            if p.get("kid") and p.get("limit"):
                sub.append(f"{p['limit']} min a day")
            if p["id"] == cur:
                sub.append("watching now")
            r = Adw.ActionRow(title=p["name"], subtitle=" · ".join(sub), activatable=True)
            r.add_prefix(Gtk.Label(label=p["emoji"], css_classes=["title-2"]))
            r.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            r.connect("activated", lambda _r, p=p: (d.close(), self.edit(p)))
            g.add(r)
        page.add(g)
        g2 = Adw.PreferencesGroup()
        add = Adw.ButtonRow(title="Add a profile", start_icon_name="list-add-symbolic") \
            if hasattr(Adw, "ButtonRow") else Adw.ActionRow(title="Add a profile", activatable=True)
        add.connect("activated", lambda *_: (d.close(), self.edit(None)))
        g2.add(add)
        pin = Adw.ActionRow(title="Grown-ups' PIN", activatable=True,
                            subtitle="Set" if profiles.has_pin(self.settings) else "Not set yet")
        pin.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        pin.connect("activated", lambda *_: self.set_pin_dialog())
        g2.add(pin)
        page.add(g2)
        tv.set_content(page)
        d.set_child(tv)
        d.present(self.win)

    def edit(self, p):
        new = p is None
        p = p or {"name": "", "emoji": profiles.EMOJIS[len(profiles.all_(self.settings)) % len(profiles.EMOJIS)],
                  "kid": False, "limit": 0}
        d = Adw.Dialog(title="New profile" if new else p["name"], content_width=460, content_height=600)
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: d.close())
        hb.pack_start(cancel)
        save = Gtk.Button(label="Save", css_classes=["suggested-action"])
        hb.pack_end(save)
        tv.add_top_bar(hb)
        page = Adw.PreferencesPage()
        g = Adw.PreferencesGroup()
        name = Adw.EntryRow(title="Name")
        name.set_text(p["name"])
        g.add(name)
        page.add(g)
        ge = Adw.PreferencesGroup(title="Picture")
        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.SINGLE, max_children_per_line=10, homogeneous=True,
                           column_spacing=4, row_spacing=4)
        for em in profiles.EMOJIS:
            lab = Gtk.Label(label=em, css_classes=["title-2"], margin_top=4, margin_bottom=4)
            flow.append(lab)
            if em == p["emoji"]:
                flow.select_child(flow.get_child_at_index(profiles.EMOJIS.index(em)))
        ge.add(flow)
        page.add(ge)
        gk = Adw.PreferencesGroup(title="Kids")
        kid = Adw.SwitchRow(title="Kids profile", subtitle="Only family films and children's channels; "
                            "nothing can be opened from outside; a PIN is needed to leave")
        kid.set_active(bool(p.get("kid")))
        gk.add(kid)
        limit = Adw.SpinRow.new_with_range(0, 600, 15)
        limit.set_title("Daily screen time")
        limit.set_subtitle("Minutes a day – 0 means no limit")
        limit.set_value(p.get("limit") or 0)
        limit.set_sensitive(kid.get_active())
        kid.connect("notify::active", lambda *_: limit.set_sensitive(kid.get_active()))
        gk.add(limit)
        page.add(gk)
        if not new:
            gd = Adw.PreferencesGroup()
            delete = Gtk.Button(label="Delete this profile", css_classes=["destructive-action", "pill"],
                                halign=Gtk.Align.CENTER)
            gd.add(delete)
            page.add(gd)

            def do_delete(*_):
                if p["id"] == self.settings.get("profile"):
                    self.win.toast("Switch to another profile first")
                    return
                if not p.get("kid") and sum(1 for x in profiles.all_(self.settings) if not x.get("kid")) <= 1:
                    self.win.toast("There must be at least one grown-up profile")
                    return
                a = Adw.AlertDialog(heading=f"Delete {p['name']}?",
                                    body="Their Recently played and resume points will be forgotten.")
                a.add_response("cancel", "Cancel")
                a.add_response("del", "Delete")
                a.set_response_appearance("del", Adw.ResponseAppearance.DESTRUCTIVE)
                a.connect("response", lambda _a, r: r == "del" and (profiles.remove(self.settings, p["id"]),
                                                                     self.win.save_settings(), d.close(),
                                                                     self.win.toast(f"{p['name']} deleted")))
                a.present(self.win)
            delete.connect("clicked", do_delete)
        tv.set_content(page)
        d.set_child(tv)

        def do_save(*_):
            sel = flow.get_selected_children()
            em = profiles.EMOJIS[sel[0].get_index()] if sel else p["emoji"]
            nm = name.get_text().strip()
            if not nm:
                self.win.toast("Give the profile a name")
                return
            is_kid = kid.get_active()
            if is_kid and not profiles.has_pin(self.settings):
                self.set_pin_dialog(then=lambda: do_save())    # kids profiles need a PIN to be locked
                return
            if is_kid and not p.get("kid") and not new and \
                    sum(1 for x in profiles.all_(self.settings) if not x.get("kid") and x["id"] != p["id"]) == 0:
                self.win.toast("There must be at least one grown-up profile")
                return
            if new:
                profiles.add(self.settings, nm, em, is_kid, int(limit.get_value()))
            else:
                live = profiles.get(self.settings, p["id"])
                live.update(name=nm[:20], emoji=em, kid=is_kid, limit=int(limit.get_value()))
            self.win.save_settings()
            d.close()
            self.apply()
            self.win.toast(f"{nm} saved")
        save.connect("clicked", do_save)
        d.present(self.win)

    # ── kids mode on / off ───────────────────────────────────────────────
    def apply(self):
        w, kid = self.win, profiles.is_kid(self.settings)
        w.kid = kid
        self._update_button()
        for name in KID_HIDDEN_PAGES:
            child = w.views.get_child_by_name(name)
            if child is not None:
                w.views.get_page(child).set_visible(not kid)
        if kid and w.views.get_visible_child_name() not in ("discover", "live"):
            w.views.set_visible_child_name("discover")
        for wid in (w.open_btn, w.menu_btn, w.cat_box, w.together.button):
            wid.set_visible(not kid)
        w.kids_badge.set_visible(kid)
        for name in KID_BLOCKED_ACTIONS:
            a = w.lookup_action(name)
            if a is not None:
                a.set_enabled(not kid)
        if kid:
            w.category = "kids"
            w.search.set_placeholder_text("Search cartoons and family films…")
            for box in (w.ai_box, w.web_box, w.doc_shelf, w.free_lbl):
                box.set_visible(False)
        else:
            w.category = self.settings.get("discover_category") or "movies"
            if w.category not in w.cat_btns:
                w.category = "movies"
            w.cat_btns[w.category].set_active(True)
            w.search.set_placeholder_text("Search free documentaries — history, space, nature, war…"
                                          if w.category == "docs" else
                                          "Search free movies — classics, cartoons, noir, open films…")
        w.livetv.set_kids(kid)
        if kid:
            w.add_css_class("kids-mode")
        else:
            w.remove_css_class("kids-mode")
        w.search.set_text("")
        w.run_search("")
        w._refresh_recent()

    # ── screen time ──────────────────────────────────────────────────────
    def tick(self, playing):
        """Called by the player a few times a second."""
        now = time.monotonic()
        last, self._last_tick = self._last_tick, now
        if not playing or last is None or not profiles.is_kid(self.settings):
            return
        left = profiles.minutes_left(self.settings)
        if left is None:
            return
        profiles.add_watch(self.settings, min(2.0, now - last))
        if now - self._saved_at > 30:
            self._saved_at = now
            self.win.save_settings()
        if left - (now - last) / 60 <= 0:
            self.times_up()
        elif 4.99 < left < 5.0 + (now - last) / 60:
            self.win.toast("5 minutes of watching left today")

    def check_before_play(self):
        left = profiles.minutes_left(self.settings)
        if left is not None and left <= 0:
            GLib.idle_add(lambda: (self.times_up(), False)[1])
            return False
        return True

    def times_up(self):
        w = self.win
        if w.root.get_visible_child_name() == "player":
            try:
                w.video.mpv.pause = True
            except Exception:
                pass
            w.leave_player()
        self.win.save_settings()
        if getattr(self, "_tu", None):
            return
        a = Adw.AlertDialog(heading="That's all for today ⭐",
                            body="You've watched all your screen time for today. See you tomorrow!")
        a.add_response("ok", "OK")
        a.add_response("more", "Grown-up: 30 more minutes")
        self._tu = a

        def resp(_a, r):
            self._tu = None
            if r == "more":
                self.ask_pin("30 more minutes", "A grown-up's PIN, please",
                             lambda: (profiles.add_bonus(self.settings, 30), self.win.save_settings(),
                                      self.win.toast("30 more minutes added")))
        a.connect("response", resp)
        a.present(w)


CSS = b"""
.avatar-btn { font-weight: 600; }
.who-btn { border-radius: 18px; padding: 10px; }
.who-face { font-size: 56px; border-radius: 26px; background: alpha(@accent_bg_color, .18); }
.kids-pill, .kids-badge { background: linear-gradient(90deg, #f5c211, #e66100); color: white; border-radius: 99px;
  padding: 2px 10px; font-weight: 800; }
.kids-mode .big-search { border: 2px solid #f5c211; }
"""

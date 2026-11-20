"""Automatic recovery from playback and download problems."""
import os
import shutil
import subprocess
import threading
import time

import gi
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib  # noqa: E402

from . import fixvideo  # noqa: E402
from .health import SAFE_MODE, log  # noqa: E402

END_ERROR = 4
MAX_ATTEMPTS = 6


class PlaybackDoctor:
    """Watches the player; when playback errors or freezes it tries a ladder of fixes."""

    def __init__(self, win):
        self.win = win
        self.src = None
        win.video.mpv.register_event_callback(self._on_event)
        self.default_hwdec = "no" if SAFE_MODE else "auto-safe"

    # called by the window whenever something new starts playing
    def start(self, kind, url, title, item=None, ch=None, stream_idx=0):
        m = self.win.video.mpv
        try:                                     # every new title starts from clean settings
            m.hwdec = self.default_hwdec
            if kind != "live":
                m.demuxer_lavf_o = ""
        except Exception:
            pass
        self.src = {"kind": kind, "url": url, "title": title, "item": item, "ch": ch,
                    "stream_idx": stream_idx, "tried": set(), "attempts": 0,
                    "started": time.time(), "last_pos": None, "last_move": time.time(),
                    "healing": False, "ok_since": None, "orig": None}
        self.win.video.recent_errors.clear()     # errors from the previous title don't count
        log.info("Play %s: %s (%s)", kind, title, url if kind != "local" else "file")

    def stop(self):
        self.src = None

    def _on_event(self, ev):                     # mpv thread
        try:
            if ev.event_id.value == 7:           # MPV_EVENT_END_FILE
                d = ev.data
                if d is not None and d.reason == END_ERROR:
                    GLib.idle_add(self.heal, f"error code {d.error}")
        except Exception:
            pass

    # called ~4×/s from the window's player tick
    def tick(self, pos, paused, cache_wait):
        s = self.src
        if not s or s["healing"]:
            return
        now = time.time()
        if s["kind"] == "local" and "convert" not in s["tried"] and self._codec_failed():
            self.heal("missing codec")
            return
        if pos is not None and pos != s["last_pos"]:
            s["last_pos"], s["last_move"] = pos, now
            if s["ok_since"] is None:
                s["ok_since"] = now
            elif now - s["ok_since"] > 60 and s["attempts"]:
                s["attempts"] = 0                # a minute of clean playback – forget old failures
            return
        if paused and not cache_wait:
            s["last_move"] = now
            return
        limit = 45 if s["kind"] == "live" else (90 if cache_wait else 25)
        if pos is None:                           # never started
            limit = 60 if s["kind"] == "live" else (12 if s["kind"] == "local" else 75)
        if now - s["last_move"] > limit:
            self.heal("frozen" if pos is not None else "won't start")

    # ── the ladder of fixes ─────────────────────────────────────────────
    def heal(self, why):
        s = self.src
        if not s or s["healing"]:
            return False
        s["attempts"] += 1
        m = self.win.video.mpv
        pos = s["last_pos"] or 0
        recent = " ".join(self.win.video.recent_errors).lower()
        log.warning("Playback problem (%s) in %s – attempt %d; player said: %s",
                    why, s["title"], s["attempts"], recent[-300:])
        if s["attempts"] > MAX_ATTEMPTS:
            return self.give_up(why)

        def step(name, cond=True):
            if cond and name not in s["tried"]:
                s["tried"].add(name)
                return True
            return False

        item, kind = s["item"], s["kind"]
        gpu_trouble = any(w in recent for w in ("hwdec", "vaapi", "vdpau", "cuda", "nvdec", "gpu", "opengl"))
        net_or_format = any(w in recent for w in ("resolve", "recognize file format", "loading failed",
                                                   "http error", "connection", "timed out", "403", "404"))
        if step("software-decoding", (gpu_trouble or (why == "frozen" and not net_or_format))
                and m.hwdec != "no"):
            m.hwdec = "no"
            return self._reload(pos, "Switching to software video decoding")
        if kind == "torrent" and item is not None:
            if step("direct-from-archive", bool(item.http_url)):
                s["url"] = item.http_url
                return self._reload(pos, "Streaming directly from the Internet Archive")
            if step("find-more-peers"):
                try:
                    item.handle.force_reannounce()
                    item.handle.force_dht_announce()
                    item.handle.clear_piece_deadlines()
                except Exception:
                    pass
                return self._reload(pos, "Looking for more sources")
        if kind == "live":
            ch = s["ch"]
            nxt = s["stream_idx"] + 1
            if ch and nxt < len(ch["streams"]) and step(f"stream-{nxt}"):
                s["stream_idx"] = nxt
                st = ch["streams"][nxt]
                s["url"] = st["url"]
                m.user_agent = st.get("ua") or "Mozilla/5.0 (X11; Linux x86_64)"
                m.referrer = st.get("ref") or ""
                return self._reload(None, "Trying the channel's backup stream")
            if step("reconnect"):
                return self._reload(None, "Reconnecting to the channel")
            if ch:
                self.win.livetv.mark_offline(ch)
            return self.give_up("the channel is off air")
        if kind == "url" and step("reconnect"):
            return self._reload(pos, "Reconnecting")
        path = s["orig"] or s["url"]
        if kind == "local" and os.path.isfile(path):
            codec = why == "missing codec" or any(w in recent for w in (
                "decoder", "codec", "unsupported", "recognize file format", "no video or audio"))
            if step("convert", codec):
                return self._fix_file(path, "convert", why)
            if step("repair-damaged-file"):
                m.demuxer_lavf_o = "fflags=+genpts+discardcorrupt"
                return self._reload(pos + 5 if pos else None, "Working around a damaged part of the file")
            if step("rebuild"):
                return self._fix_file(path, "repair", why, pos)
            if step("convert"):
                return self._fix_file(path, "convert", why)
        if step("repair-damaged-file"):
            m.demuxer_lavf_o = "fflags=+genpts+discardcorrupt"
            return self._reload(pos + 5 if pos else None, "Working around a damaged part of the file")
        if step("skip-ahead", pos > 0):
            return self._reload(pos + 30, "Skipping past a damaged section")
        return self.give_up(why)

    def _codec_failed(self):
        """A decoder is missing AND the picture (or, for audio-only files, the sound) isn't playing."""
        errs = " ".join(self.win.video.recent_errors).lower()
        if not any(w in errs for w in ("failed to initialize a decoder", "could not find decoder",
                                       "no decoder", "unsupported codec", "codec not supported")):
            return False
        m = self.win.video.mpv
        try:
            tracks = m.track_list or []
            has_video = any(t.get("type") == "video" and not t.get("albumart") for t in tracks)
            if has_video:
                return not m.video_params
            return not m.audio_params
        except Exception:
            return True

    def _fix_file(self, path, mode, why, pos=None):
        """Repair or convert the file in the background; play the result as soon as it can."""
        s = self.src
        s["healing"] = True
        s["orig"] = path
        win = self.win
        title = "Making this video playable…"
        log.info("Fix: %s %s (%s)", mode, s["title"], why)
        win.set_buffering(title, "Checking the video format…", None)
        alive = lambda: self.src is s  # noqa: E731

        def status(text, frac=None):
            GLib.idle_add(lambda: alive() and s["healing"] and win.set_buffering(title, text, frac) and False)

        def play(target, subs=(), message="", at=None):
            if not alive():
                return False
            s["url"] = target
            m = win.video.mpv
            m.demuxer_lavf_o = ""
            s["healing"] = False
            self._reload(at, message)
            for sub in subs:
                GLib.timeout_add(1500, lambda f=sub: (m.command_async("sub-add", f, "auto"), False)[1])
            win.toast(message)
            return False

        def work():
            try:
                if mode == "convert":
                    fixvideo.convert_live(
                        path, stop=lambda: not alive(), status=status,
                        ready=lambda pl, subs: GLib.idle_add(
                            play, pl, subs, "Converted on the fly – playing while the rest converts"))
                else:
                    out = fixvideo.make_playable(path, "repair", progress=status, stop=lambda: not alive())
                    GLib.idle_add(play, out, (), "Rebuilt the damaged file – playing now", pos)
            except fixvideo.DRMError as e:
                GLib.idle_add(lambda: alive() and self.give_up(str(e), drm=True) and False)
            except InterruptedError:
                pass
            except Exception as e:
                log.error("Couldn't %s %s: %s", mode, path, e)

                def next_fix():
                    if alive():
                        s["healing"] = False
                        self.heal(why)
                    return False
                GLib.idle_add(next_fix)

        threading.Thread(target=work, daemon=True).start()
        return False

    def _reload(self, pos, message):
        s = self.src
        s["healing"] = True
        log.info("Fix: %s (%s)", message, s["title"])
        self.win.set_buffering("Fixing a playback problem…", message, None)
        m = self.win.video.mpv

        def go():
            try:
                if pos:
                    m.loadfile(s["url"], start=f"{pos:.1f}")
                else:
                    m.loadfile(s["url"])
                m.pause = False
            except Exception as e:
                log.error("Reload failed: %s", e)
            s["last_move"] = time.time()
            s["last_pos"] = None
            s["ok_since"] = None
            s["healing"] = False
            return False
        GLib.timeout_add(600, go)
        return False

    def give_up(self, why, drm=False):
        s = self.src
        if not s:
            return False
        log.error("Gave up on %s (%s)", s["title"], why)
        self.src = None
        win = self.win
        win.set_buffering(None, None, None)
        vlc = shutil.which("vlc")
        local = s["kind"] in ("local", "url") or (s["kind"] == "torrent" and s["item"] is not None)
        body = why if drm else ("CineHaven tried every automatic fix – including downloading the full set of"
                                " video codecs and converting the file – but this one still won't play"
                                f" ({why}). The file may be badly damaged or incomplete. The details are in"
                                " the log (Menu → Help & diagnostics).")
        dlg = Adw.AlertDialog(heading=f"Couldn't play “{s['title']}”", body=body)
        dlg.add_response("close", "Close")
        dlg.add_response("retry", "Try again")
        if vlc and local:
            dlg.add_response("vlc", "Open in VLC")
        dlg.set_default_response("retry")

        def resp(_d, r):
            if r == "retry":
                self.start(s["kind"], s["url"], s["title"], s["item"], s["ch"], 0)
                self.src["last_pos"] = s["last_pos"]
                self._reload(s["last_pos"], "Trying again")
            elif r == "vlc":
                target = s["url"]
                if s["kind"] == "torrent" and s["item"] is not None:
                    target = s["item"].http_url or s["item"].stream_url()
                subprocess.Popen([vlc, target], start_new_session=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                win.leave_player()
            else:
                win.leave_player()
        dlg.connect("response", resp)
        dlg.present(win)
        return False


class DownloadDoctor:
    """Every minute: un-sticks errored or stalled downloads (runs on the GLib main loop)."""

    def __init__(self, win):
        self.win = win
        self.state = {}                    # key -> {"bytes":, "since":, "fixes":}
        GLib.timeout_add_seconds(60, self.round)

    def round(self):
        eng = self.win.engine
        for it in list(eng.items.values()):
            try:
                self._check(it)
            except Exception as e:
                log.warning("Download check failed for %s: %s", it.title, e)
        return True

    def _check(self, it):
        h = it.handle
        if not h.is_valid() or it.is_paused():
            return
        st = h.status()
        rec = self.state.setdefault(it.key, {"bytes": st.total_done, "since": time.time(), "fixes": 0})
        if st.errc and st.errc.value():
            msg = st.errc.message()
            log.warning("Download error in %s: %s", it.title, msg)
            if "space" in msg.lower():
                it.pause()
                self.win.toast("Disk full – downloads paused. Free some space, then press resume in Library.")
                return
            if rec["fixes"] < 3:
                rec["fixes"] += 1
                h.clear_error()
                if "file" in msg.lower() or "read" in msg.lower():
                    h.force_recheck()
                h.resume()
                self.win.toast(f"Fixed a download problem in “{it.title}”")
            return
        if it.file_index is None or it.is_complete():
            rec.update(bytes=st.total_done, since=time.time())
            return
        if st.total_done > rec["bytes"]:
            rec.update(bytes=st.total_done, since=time.time())
            return
        stalled = time.time() - rec["since"]
        if stalled > 300 and not rec.get("kicked"):
            rec["kicked"] = True
            log.info("Stalled %ds – re-announcing %s", stalled, it.title)
            h.force_reannounce()
            h.force_dht_announce()
        if stalled > 900 and it.http_url and it.key not in self.win.verifying:
            log.info("Stalled %ds – finishing %s straight from the Archive", stalled, it.title)
            rec["since"] = time.time()
            self.win.finish_from_archive(it)

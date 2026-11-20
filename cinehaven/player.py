"""libmpv embedded in a Gtk.GLArea (all FFmpeg codecs, hardware decoding)."""
import collections
import ctypes
import ctypes.util
import locale

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

import mpv  # noqa: E402

GL_FRAMEBUFFER_BINDING = 0x8CA6


def _load(*names):
    for n in names:
        try:
            return ctypes.CDLL(n)
        except OSError:
            continue
    return None


_egl = _load("libEGL.so.1", "libEGL.so")
_glx = _load("libGL.so.1", "libGL.so")
_gl = _load("libOpenGL.so.0", "libGL.so.1", "libGL.so")
if _egl:
    _egl.eglGetProcAddress.restype = ctypes.c_void_p
    _egl.eglGetProcAddress.argtypes = [ctypes.c_char_p]
if _glx:
    _glx.glXGetProcAddressARB.restype = ctypes.c_void_p
    _glx.glXGetProcAddressARB.argtypes = [ctypes.c_char_p]
_gl.glGetIntegerv.argtypes = [ctypes.c_uint, ctypes.POINTER(ctypes.c_int)]


def _get_proc(_ctx, name):
    addr = _egl.eglGetProcAddress(name) if _egl else None
    if not addr and _glx:
        addr = _glx.glXGetProcAddressARB(name)
    return addr or 0


class VideoView(Gtk.GLArea):
    def __init__(self):
        super().__init__()
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_auto_render(False)
        self.set_has_depth_buffer(False)
        self.set_has_stencil_buffer(False)
        locale.setlocale(locale.LC_NUMERIC, "C")   # libmpv requirement
        from .health import SAFE_MODE, log
        self._log = log
        self.recent_errors = collections.deque(maxlen=12)
        self.mpv = mpv.MPV(
            log_handler=self._on_mpv_log, loglevel="warn",
            vo="libmpv", hwdec="no" if SAFE_MODE else "auto-safe", keep_open="yes", idle="yes",
            input_default_bindings=False, input_vo_keyboard=False, osc=False,
            cache="yes", demuxer_max_bytes="200MiB", demuxer_readahead_secs=30,
            network_timeout=120, sub_auto="fuzzy", slang="en,eng", alang="en,eng",
            ytdl=False, osd_level=1, osd_font_size=38, volume=100, volume_max=150,
            user_agent="CineHaven")
        self.ctx = None
        self._proc_fn = mpv.MpvGlGetProcAddressFn(_get_proc)
        self.connect("realize", self._on_realize)
        self.connect("unrealize", self._on_unrealize)
        self.connect("render", self._on_render)

    def _on_mpv_log(self, level, component, message):
        msg = f"[{component}] {message.strip()}"
        if level in ("error", "fatal") or "hwdec" in message or "fail" in message.lower():
            self.recent_errors.append(msg)
        if level in ("error", "fatal"):
            self._log.warning("mpv %s", msg)

    def _on_realize(self, area):
        area.make_current()
        if area.get_error():
            print("GL error:", area.get_error())
            return
        self.ctx = mpv.MpvRenderContext(
            self.mpv, "opengl", opengl_init_params={"get_proc_address": self._proc_fn})
        self.ctx.update_cb = lambda: GLib.idle_add(self.queue_render)

    def _on_unrealize(self, area):
        if self.ctx:
            area.make_current()
            self.ctx.free()
            self.ctx = None

    def _on_render(self, area, _glctx):
        if not self.ctx:
            return False
        fbo = ctypes.c_int(0)
        _gl.glGetIntegerv(GL_FRAMEBUFFER_BINDING, ctypes.byref(fbo))
        sf = self.get_scale_factor()
        w, h = self.get_width() * sf, self.get_height() * sf
        self.ctx.render(flip_y=True, opengl_fbo={"w": w, "h": h, "fbo": fbo.value})
        return True

    # -- convenience --------------------------------------------------------
    def play(self, url, subs=(), start=None):
        if start:
            self.mpv.loadfile(url, start=f"{start:.1f}")
        else:
            self.mpv.loadfile(url)
        self.mpv.pause = False
        for s in subs:
            try:
                self.mpv.command_async("sub-add", s, "auto")
            except Exception:
                pass

    def stop(self):
        try:
            self.mpv.command("stop")
        except Exception:
            pass

    def shutdown(self):
        try:
            self.stop()
            if self.ctx:
                self.make_current()
                self.ctx.free()
                self.ctx = None
            self.mpv.terminate()
        except Exception:
            pass

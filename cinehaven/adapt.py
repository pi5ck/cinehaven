"""Self-tuning: CineHaven finds out what this computer can do, learns from every film it
plays, and keeps an eye on new video technology.

1. Probe – which video formats the graphics chip really decodes (tested with tiny clips,
   in a separate process so a broken driver can't take the app down), and what's missing
   (for example a hardware-decoding driver that was never installed). Runs again by itself
   when the graphics chip, its driver or mpv change, and otherwise once a week.
2. Learn – for each kind of video (format × resolution × hardware or software decoding)
   it remembers the richest picture-quality level that played without dropping frames and
   starts there next time. Levels that stuttered are tried again after a month, or at once
   when the computer, driver or player changes – a faster setup gets the better picture.
3. Watch – once a week it looks up the newest mpv and FFmpeg (the engines that decode and
   draw the picture) and says what's newer than what this computer has.

Nothing here changes CineHaven's own code: new features still arrive as checked, signed-off
releases through the updater.
"""
import glob
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

from . import DATA_DIR, USER_AGENT
from .health import load_json, log, save_json

STATE = os.path.join(DATA_DIR, "adapt.json")
WEEK = 7 * 24 * 3600
RETRY_FAILED_AFTER = 30 * 24 * 3600

# picture-quality levels (see enhance.apply_picture)
LEVELS = {1: "Light", 2: "Enhanced", 3: "Best"}
TOP = 3

# name, label, ffmpeg encoder arguments for a tiny test clip
CODECS = (
    ("h264", "H.264", ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]),
    ("hevc", "HEVC (H.265)", ["-c:v", "libx265", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                              "-x265-params", "log-level=error"]),
    ("hevc10", "HEVC 10-bit (HDR films)", ["-c:v", "libx265", "-preset", "ultrafast", "-pix_fmt", "yuv420p10le",
                                           "-x265-params", "log-level=error"]),
    ("vp9", "VP9 (YouTube)", ["-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-pix_fmt", "yuv420p"]),
    ("av1", "AV1 (newest streaming format)", ["-c:v", "libsvtav1", "-preset", "12", "-pix_fmt", "yuv420p"]),
)
AV1_FALLBACK = ["-c:v", "libaom-av1", "-cpu-used", "8", "-row-mt", "1", "-pix_fmt", "yuv420p"]

VENDORS = {"0x8086": "Intel", "0x1002": "AMD", "0x10de": "NVIDIA"}

_lock = threading.Lock()


def _state():
    return load_json(STATE, {})


def _save(st):
    with _lock:
        save_json(STATE, st)


# ── what's in this computer ───────────────────────────────────────────────
def gpus():
    """[(vendor, pci id, name)] for the graphics chips the system can see (Linux)."""
    out = []
    for card in sorted(glob.glob("/sys/class/drm/card[0-9]")):
        try:
            ven = open(os.path.join(card, "device/vendor")).read().strip()
            dev = open(os.path.join(card, "device/device")).read().strip()
        except OSError:
            continue
        name = ""
        if shutil.which("lspci"):
            try:
                r = subprocess.run(["lspci", "-mm", "-d", f"{ven[2:]}:{dev[2:]}"], capture_output=True,
                                   text=True, timeout=5).stdout.splitlines()
                f = re.findall(r'"([^"]*)"', r[0]) if r else []
                name = f[2] if len(f) > 2 else ""
            except Exception:
                pass
        out.append((VENDORS.get(ven, ven), f"{ven}:{dev}", name))
    return out


def _driver_files():
    """Names of the installed hardware-decoding drivers (part of the fingerprint)."""
    pats = ["/usr/lib*/dri/*_drv_video.so", "/usr/lib/*/dri/*_drv_video.so", "/usr/lib*/vdpau/*.so*",
            "/usr/lib/*/vdpau/*.so*", "/usr/lib/*/libnvcuvid.so*", "/usr/lib*/libnvcuvid.so*"]
    return sorted({os.path.basename(p) for pat in pats for p in glob.glob(pat)})


_versions = None


def _mpv_versions():
    global _versions
    if _versions:
        return _versions
    try:
        import mpv
        h = mpv.MPV(vo="null", ao="null", idle="yes")
        v = (h.mpv_version or "", h.ffmpeg_version or "")
        h.terminate()
        _versions = v
        return v
    except Exception:
        return ("", "")


def fingerprint():
    """Changes when anything that affects video decoding changes."""
    parts = [platform.machine(), platform.system(), repr(gpus()), repr(_driver_files()), repr(_mpv_versions())]
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


# ── testing hardware decoding ─────────────────────────────────────────────
_CHILD = r"""
import json, locale, sys, time
locale.setlocale(locale.LC_NUMERIC, "C")
import mpv
m = mpv.MPV(vo="null", ao="null", hwdec="auto-copy-safe", idle="yes", keep_open="yes", loglevel="no")
res = {}
for name, path in json.loads(sys.argv[1]).items():
    m.loadfile(path)
    t, cur = time.time(), None
    while time.time() - t < 6:          # decide only once frames are really coming out: mpv names
        try:                            # the hardware decoder while it tries it, before falling back
            pos = m.time_pos or 0
        except Exception:
            pos = 0
        if pos > 0.3:
            break
        time.sleep(0.05)
    try:
        cur = m.hwdec_current
    except Exception:
        cur = None
    res[name] = cur if cur and cur != "no" else ""
    m.command("stop")
m.terminate()
print("RESULT" + json.dumps(res))
"""


def _make_clips(folder):
    from .skip import _ffmpeg
    exe = _ffmpeg()
    if not exe:
        return {}
    clips = {}
    for name, _label, args in CODECS:
        out = os.path.join(folder, f"{name}.mkv")
        for a in ([args, AV1_FALLBACK] if name == "av1" else [args]):
            try:
                r = subprocess.run([exe, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=24:d=1.2"]
                                   + a + [out], capture_output=True, timeout=60)
                if r.returncode == 0 and os.path.getsize(out) > 0:
                    clips[name] = out
                    break
            except Exception:
                pass
    return clips


def probe_decoding():
    """{codec: hardware decoder name or ""} for each codec that could be tested."""
    with tempfile.TemporaryDirectory(prefix="cinehaven-probe-") as d:
        clips = _make_clips(d)
        if not clips:
            return {}
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
        try:
            r = subprocess.run([sys.executable, "-c", _CHILD, json.dumps(clips)], capture_output=True,
                               text=True, timeout=90, env=env)
        except Exception as e:
            log.info("Decoding test didn't run: %s", e)
            return {}
        m = re.search(r"RESULT(\{.*\})", r.stdout)
        if not m:
            log.info("Decoding test gave no answer (exit %s)", r.returncode)
            return {}
        return json.loads(m.group(1))


def _pkg_manager():
    for exe, cmd in (("apt-get", "apt-get install -y"), ("dnf", "dnf install -y"), ("pacman", "pacman -S --noconfirm"),
                     ("zypper", "zypper install -y")):
        if shutil.which(exe):
            return exe, cmd
    return None, None


DRIVER_PACKAGES = {
    "Intel": {"apt-get": "intel-media-va-driver", "dnf": "intel-media-driver", "pacman": "intel-media-driver",
              "zypper": "intel-media-driver"},
    "AMD": {"apt-get": "mesa-va-drivers", "dnf": "mesa-va-drivers", "pacman": "libva-mesa-driver",
            "zypper": "Mesa-dri"},
}


def advice(info):
    """Plain-language suggestions: [{title, text, command?}]."""
    tips = []
    hw = info.get("decode") or {}
    tested = [c for c in hw]
    if not tested:
        return tips
    vendors = [g[0] for g in info.get("gpus", [])]
    if not any(hw.values()) and sys.platform.startswith("linux"):
        vendor = next((v for v in vendors if v in ("Intel", "AMD", "NVIDIA")), None)
        mgr, cmd = _pkg_manager()
        pkg = DRIVER_PACKAGES.get(vendor, {}).get(mgr)
        if vendor == "NVIDIA":
            tips.append({"title": "Install the NVIDIA driver",
                         "text": "Your NVIDIA graphics card can decode video, but CineHaven couldn't use it – the "
                                 "proprietary NVIDIA driver doesn't seem to be installed. Install it from your "
                                 "system's \"Additional Drivers\" settings. Films will play cooler and quieter."})
        elif vendor:
            t = {"title": f"Let the {vendor} graphics chip decode video",
                 "text": f"Every film is being decoded by the processor, because the {vendor} video driver isn't "
                         "installed. With it, playback uses far less power, the fan stays quiet, and big 4K "
                         "and HDR films play smoothly."}
            if pkg:
                t["command"] = f"{cmd} {pkg}"
                t["package"] = pkg
            tips.append(t)
    elif hw and not hw.get("av1") and "av1" in hw and any(hw.values()):
        tips.append({"title": "AV1 is decoded by the processor",
                     "text": "This graphics chip is older than the AV1 format (used by newer streams and YouTube "
                             "in 4K). It still plays, but uses more power. Graphics chips from 2021 on decode it."})
    if hw and "hevc10" in hw and not hw.get("hevc10") and any(hw.values()):
        tips.append({"title": "10-bit HDR films use the processor",
                     "text": "The graphics chip can't decode 10-bit HEVC, which most 4K HDR films use."})
    return tips


def probe(force=False):
    """Run (or reuse) the hardware probe. Returns the stored info dict."""
    st = _state()
    info = st.get("probe") or {}
    fp = fingerprint()
    if not force and info.get("fp") == fp and time.time() - info.get("t", 0) < WEEK:
        return info
    t0 = time.time()
    mpv_v, ff_v = _mpv_versions()
    info = {"fp": fp, "t": time.time(), "gpus": gpus(), "cpu_threads": os.cpu_count() or 1,
            "mpv": mpv_v, "ffmpeg": ff_v, "decode": probe_decoding()}
    info["advice"] = advice(info)
    st = _state()
    if (st.get("probe") or {}).get("fp") not in (None, fp):
        # something changed (new driver, graphics card or player): retry every quality level
        st["learn"] = {k: dict(v, fail=None) for k, v in (st.get("learn") or {}).items()}
        log.info("Video setup changed – picture quality levels will be re-learnt")
    st["probe"] = info
    _save(st)
    log.info("Video probe took %.1f s: %s", time.time() - t0,
             ", ".join(f"{k}={v or 'software'}" for k, v in info["decode"].items()) or "nothing tested")
    return info


# ── learning the best picture level for each kind of video ────────────────
def video_class(m):
    """e.g. 'hevc:2160:hw' – the kind of video that's playing."""
    try:
        codec = (m.video_format or "?").split()[0].lower()
        h = (m.video_params or {}).get("h") or 0
        hw = (m.hwdec_current or "no") not in ("no", "")
    except Exception:
        return None
    if not h:
        return None
    rung = next((r for r in (480, 576, 720, 1080, 1440, 2160) if h <= r * 1.05), 4320)
    return f"{codec}:{rung}:{'hw' if hw else 'sw'}"


def start_level(cls):
    """The richest level not known to stutter for this kind of video."""
    rec = (_state().get("learn") or {}).get(cls) or {}
    fail = rec.get("fail")
    if fail and time.time() - rec.get("fail_t", 0) > RETRY_FAILED_AFTER:
        fail = None
    return max(1, min(TOP, (fail - 1) if fail else TOP))


def record_drop(cls, level):
    """Frames were dropped at this level: remember, so the next film of this kind starts lower."""
    if not cls:
        return
    st = _state()
    rec = st.setdefault("learn", {}).setdefault(cls, {})
    if not rec.get("fail") or level < rec["fail"]:
        rec["fail"] = level
    rec["fail_t"] = time.time()
    if (rec.get("ok") or 0) >= level:
        rec["ok"] = level - 1
    _save(st)


def record_smooth(cls, level, seconds):
    """Played this long without trouble at this level."""
    if not cls or seconds < 120:
        return
    st = _state()
    rec = st.setdefault("learn", {}).setdefault(cls, {})
    rec["ok"] = max(rec.get("ok") or 0, level)
    rec["seconds"] = int(rec.get("seconds", 0) + seconds)
    _save(st)


def forget():
    st = _state()
    st.pop("learn", None)
    _save(st)


def learned():
    return _state().get("learn") or {}


def describe_class(cls):
    codec, rung, hw = (cls.split(":") + ["", "", ""])[:3]
    names = {"h264": "H.264", "hevc": "HEVC", "vp9": "VP9", "av1": "AV1", "mpeg2video": "MPEG-2",
             "mpeg4": "MPEG-4", "vc1": "VC-1"}
    r = {"2160": "4K", "4320": "8K"}.get(rung, f"{rung}p")
    return f"{r} {names.get(codec, codec.upper())} · {'graphics chip' if hw == 'hw' else 'processor'}"


# ── keeping up with video technology ──────────────────────────────────────
def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def _ver(s):
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", s or "")
    return tuple(int(x or 0) for x in m.groups()) if m else ()


def latest_upstream():
    """{'mpv': (version, url), 'ffmpeg': (version, url)} from their public repositories."""
    out = {}
    try:
        r = _get_json("https://api.github.com/repos/mpv-player/mpv/releases/latest")
        out["mpv"] = (r["tag_name"].lstrip("v"), r["html_url"])
    except Exception as e:
        log.info("mpv release check failed: %s", e)
    try:
        tags = _get_json("https://api.github.com/repos/FFmpeg/FFmpeg/tags?per_page=100")
        rel = [t["name"] for t in tags if re.fullmatch(r"n\d+\.\d+(\.\d+)?", t["name"])]
        if rel:
            best = max(rel, key=_ver)
            out["ffmpeg"] = (best[1:], "https://ffmpeg.org/download.html#releases")
    except Exception as e:
        log.info("FFmpeg release check failed: %s", e)
    return out


def tech_news(force=False):
    """[{name, have, latest, url, newer}] – cached for a week."""
    st = _state()
    w = st.get("watch") or {}
    if force or time.time() - w.get("t", 0) > WEEK or not w.get("latest"):
        latest = latest_upstream()
        if latest:
            w = {"t": time.time(), "latest": latest}
            st["watch"] = w
            _save(st)
    mpv_v, ff_v = _mpv_versions()
    have = {"mpv": mpv_v, "ffmpeg": ff_v}
    rows = []
    for key, name in (("mpv", "mpv – plays and draws the picture"), ("ffmpeg", "FFmpeg – decodes every format")):
        lat = (w.get("latest") or {}).get(key)
        hv = have.get(key) or ""
        rows.append({"name": name, "have": _ver(hv) and ".".join(map(str, _ver(hv))) or "?",
                     "latest": lat[0] if lat else None, "url": lat[1] if lat else None,
                     "newer": bool(lat and _ver(hv) and _ver(lat[0]) > _ver(hv))})
    return rows


def how_to_update_engines():
    if sys.platform.startswith("win") or sys.platform == "darwin":
        return "They come bundled with CineHaven: the next CineHaven update brings the newest versions."
    mgr, _ = _pkg_manager()
    if mgr == "apt-get":
        return "They come from your Linux system: they're updated with your system updates (Software Updater)."
    return "They come from your Linux system and are updated with your system updates."


def install_driver(package):
    """Install a driver package with the system's password prompt. Returns (ok, message)."""
    mgr, cmd = _pkg_manager()
    if not mgr or not shutil.which("pkexec"):
        return False, "Can't ask for the administrator password here – run the command in a terminal."
    try:
        r = subprocess.run(["pkexec"] + cmd.split() + [package], capture_output=True, text=True, timeout=900)
    except Exception as e:
        return False, str(e)
    if r.returncode == 0:
        return True, "Installed. Restart CineHaven to use it."
    if r.returncode in (126, 127):
        return False, "Cancelled."
    return False, (r.stderr or r.stdout).strip().splitlines()[-1:][0] if (r.stderr or r.stdout).strip() else \
        f"The installer stopped (code {r.returncode})."

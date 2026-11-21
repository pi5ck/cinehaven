"""Make any video file playable: diagnose it, fetch codecs if needed, repair or convert it.

Used by the playback doctor when a file won't play:
  1. ffprobe identifies the container and codecs (and spots DRM-locked files).
  2. If no FFmpeg on this computer can decode it, the full static FFmpeg build
     (github.com/BtbN/FFmpeg-Builds, every codec FFmpeg supports) is downloaded once,
     checked against its published SHA-256, and kept in CineHaven's data folder.
  3. "repair": streams are copied into a fresh MKV with timestamps rebuilt and damaged
     packets dropped – fixes broken containers in seconds.
  4. "convert": video re-encoded to H.264 and audio to AAC, which every player decodes.
Converted copies live in ~/.local/share/cinehaven/converted and are removed after 30 days.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import time
import urllib.request

from . import DATA_DIR
from .health import log

TOOLS = os.path.join(DATA_DIR, "tools")
OLD_OUT_DIR = os.path.join(DATA_DIR, "converted")


def out_dir():
    """Repaired copies live with the films (Menu → Where films are kept), not on the system disk."""
    import cinehaven
    return os.path.join(cinehaven.DOWNLOAD_DIR, ".repaired")
BUILD = "ffmpeg-master-latest-linux64-gpl"
BUILD_URL = f"https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/{BUILD}.tar.xz"
SUMS_URL = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/checksums.sha256"
KEEP_DAYS = 30
DRM_TAGS = {"encv", "enca", "drmi", "drms", "drmz", "p608"}


class DRMError(Exception):
    pass


def _static():
    p = os.path.join(TOOLS, BUILD, "bin")
    return (os.path.join(p, "ffmpeg"), os.path.join(p, "ffprobe")) if os.path.isfile(os.path.join(p, "ffmpeg")) else None


def _system():
    a, b = shutil.which("ffmpeg"), shutil.which("ffprobe")
    return (a, b) if a and b else None


def _decoders(ffmpeg):
    try:
        out = subprocess.run([ffmpeg, "-hide_banner", "-decoders"], capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return set()
    return {m.group(1) for m in re.finditer(r"^\s[VAS][\w.]{5}\s+(\S+)", out, re.M)}


def download_ffmpeg(progress=None):
    """Download + verify + unpack the full static FFmpeg. Returns (ffmpeg, ffprobe)."""
    os.makedirs(TOOLS, exist_ok=True)
    req = lambda u: urllib.request.Request(u, headers={"User-Agent": "CineHaven"})  # noqa: E731
    with urllib.request.urlopen(req(SUMS_URL), timeout=30) as r:
        sums = r.read().decode()
    expected = next((ln.split()[0] for ln in sums.splitlines() if ln.strip().endswith(f"{BUILD}.tar.xz")), None)
    if not expected:
        raise RuntimeError("FFmpeg download has no published checksum")
    tmp = os.path.join(TOOLS, f"{BUILD}.tar.xz.part")
    h = hashlib.sha256()
    with urllib.request.urlopen(req(BUILD_URL), timeout=60) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            got += len(chunk)
            if progress and total:
                progress(f"Downloading video codecs… {got * 100 // total}%", got / total)
    if h.hexdigest() != expected:
        os.remove(tmp)
        raise RuntimeError("Codec download failed its checksum – not installed")
    if progress:
        progress("Installing video codecs…", None)
    shutil.rmtree(os.path.join(TOOLS, BUILD), ignore_errors=True)
    with tarfile.open(tmp) as t:
        t.extractall(TOOLS, filter="data")
    os.remove(tmp)
    root = os.path.join(TOOLS, BUILD)
    for e in os.scandir(root):                 # keep only bin/ffmpeg + bin/ffprobe
        if e.name != "bin":
            shutil.rmtree(e.path, ignore_errors=True) if e.is_dir() else os.remove(e.path)
    for e in os.scandir(os.path.join(root, "bin")):
        if e.name not in ("ffmpeg", "ffprobe"):
            os.remove(e.path)
    log.info("Installed static FFmpeg to %s", TOOLS)
    return _static()


def probe(path, tools=None):
    tools = tools or _static() or _system()
    if not tools:
        return None
    try:
        out = subprocess.run([tools[1], "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
                             capture_output=True, text=True, timeout=60)
        return json.loads(out.stdout or "{}")
    except Exception as e:
        log.warning("ffprobe failed: %s", e)
        return None


def describe(info):
    if not info:
        return "unknown format"
    fmt = (info.get("format") or {}).get("format_long_name") or (info.get("format") or {}).get("format_name", "?")
    codecs = [f"{s.get('codec_type')}: {s.get('codec_name') or s.get('codec_tag_string')}" for s in info.get("streams", [])]
    return f"{fmt} ({', '.join(codecs)})"


def tools_for(path, progress=None):
    """An FFmpeg that can decode every stream in `path`, downloading one if needed."""
    cands = [t for t in (_static(), _system()) if t]
    info = probe(path, cands[0]) if cands else None
    if info:
        tags = {(s.get("codec_tag_string") or "").lower() for s in info.get("streams", [])}
        if tags & DRM_TAGS:
            raise DRMError("This file is copy-protected (DRM). It can only be played in the app it was bought from.")
        needed = {s.get("codec_name") for s in info.get("streams", [])
                  if s.get("codec_type") in ("video", "audio") and s.get("codec_name")}
        for t in cands:
            if needed <= _decoders(t[0]):
                return t, info
        log.info("No local FFmpeg decodes %s – fetching the full build", ", ".join(sorted(needed)))
    if not _static():
        t = download_ffmpeg(progress)
        return t, probe(path, t)
    return _static(), info


def _out_path(path, mode):
    st = os.stat(path)
    key = hashlib.sha1(f"{os.path.realpath(path)}|{st.st_size}|{st.st_mtime}|{mode}".encode()).hexdigest()[:16]
    return os.path.join(out_dir(), f"{key}.mkv")


def make_playable(path, mode, progress=None, stop=lambda: False):
    """mode 'repair' (fast stream copy) or 'convert' (H.264/AAC). Returns the playable file path."""
    os.makedirs(out_dir(), exist_ok=True)
    out = _out_path(path, mode)
    if os.path.exists(out) and os.path.getsize(out) > 0:
        return out
    tools, info = tools_for(path, progress)
    log.info("Making playable (%s): %s – %s", mode, path, describe(info))
    duration = float((info or {}).get("format", {}).get("duration") or 0)
    common = [tools[0], "-hide_banner", "-nostdin", "-y", "-fflags", "+genpts+discardcorrupt",
              "-err_detect", "ignore_err", "-i", path, "-map", "0:v:0?", "-map", "0:a?", "-map", "0:s?",
              "-ignore_unknown"]
    if mode == "repair":
        cmd = common + ["-c", "copy"]
    else:
        cmd = common + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-c:s", "srt"]
    tmp = out + ".part.mkv"
    for attempt in (cmd, [c for c in cmd if c not in ("-map", "0:s?", "-c:s", "srt")]):  # 2nd try: no subtitles
        proc = subprocess.Popen(attempt + ["-progress", "pipe:1", "-nostats", tmp],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for line in proc.stdout:
            if stop():
                proc.kill()
                raise InterruptedError("stopped")
            if line.startswith("out_time_us=") and duration and progress:
                try:
                    done = int(line.split("=")[1]) / 1e6 / duration
                    label = "Repairing the file" if mode == "repair" else "Converting to a playable format"
                    progress(f"{label}… {min(done, 1) * 100:.0f}%", min(done, 1.0))
                except ValueError:
                    pass
        err = proc.stderr.read()
        if proc.wait() == 0 and os.path.getsize(tmp) > 0:
            os.replace(tmp, out)
            return out
        log.warning("ffmpeg %s failed: %s", mode, err[-400:])
    raise RuntimeError(f"Couldn't {mode} this file ({describe(info)})")


def convert_live(path, progress=None, stop=lambda: False, ready=None, status=None):
    """Convert to H.264/AAC as a growing HLS playlist, so playback can start within seconds.

    ready(playlist, subtitle_files) is called as soon as the first segments exist (or at
    once if a finished conversion is cached). Returns the playlist path when done."""
    key = os.path.splitext(os.path.basename(_out_path(path, "live")))[0]
    d = os.path.join(out_dir(), key)
    pl = os.path.join(d, "index.m3u8")
    subs_of = lambda: sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith(".srt"))  # noqa: E731
    if os.path.exists(pl) and "#EXT-X-ENDLIST" in open(pl).read():
        if ready:
            ready(pl, subs_of())
        return pl
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    tools, info = tools_for(path, status)
    if status:
        status("Converting to a playable format…", None)
    log.info("Converting for playback: %s – %s", path, describe(info))
    duration = float((info or {}).get("format", {}).get("duration") or 0)
    # text subtitles → .srt files next to the playlist (quick, done first)
    text_subs = [s for s in (info or {}).get("streams", []) if s.get("codec_type") == "subtitle"
                 and s.get("codec_name") in ("subrip", "ass", "ssa", "mov_text", "webvtt", "text")][:4]
    for i, st in enumerate(text_subs):
        lang = (st.get("tags") or {}).get("language", "und")
        subprocess.run([tools[0], "-hide_banner", "-nostdin", "-y", "-i", path, "-map", f"0:{st['index']}",
                        os.path.join(d, f"sub{i}.{lang}.srt")], capture_output=True, timeout=300)
    cmd = [tools[0], "-hide_banner", "-nostdin", "-y", "-fflags", "+genpts+discardcorrupt",
           "-err_detect", "ignore_err", "-i", path, "-map", "0:v:0?", "-map", "0:a:0?",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
           "-force_key_frames", "expr:gte(t,n_forced*4)",
           "-c:a", "aac", "-b:a", "192k", "-ac", "2",
           "-f", "hls", "-hls_time", "4", "-hls_playlist_type", "event",
           "-hls_segment_filename", os.path.join(d, "seg%05d.ts"),
           "-progress", "pipe:1", "-nostats", pl]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    announced = False
    try:
        for line in proc.stdout:
            if stop():
                proc.kill()
                raise InterruptedError("stopped")
            if not announced and os.path.exists(pl) and open(pl).read().count("#EXTINF") >= 2:
                announced = True
                if ready:
                    ready(pl, subs_of())
            if line.startswith("out_time_us=") and duration and progress:
                try:
                    progress(min(int(line.split("=")[1]) / 1e6 / duration, 1.0))
                except ValueError:
                    pass
        err = proc.stderr.read()
        if proc.wait() != 0:
            raise RuntimeError(f"Couldn't convert this file ({describe(info)}): {err[-300:]}")
    finally:
        if proc.poll() is None:
            proc.kill()
    if not announced and ready:
        ready(pl, subs_of())
    return pl


def cleanup():
    """Remove converted copies older than 30 days."""
    try:
        cutoff = time.time() - KEEP_DAYS * 86400
        for e in os.scandir(out_dir()):
            if e.stat().st_mtime < cutoff:
                shutil.rmtree(e.path) if e.is_dir() else os.remove(e.path)
    except OSError:
        pass

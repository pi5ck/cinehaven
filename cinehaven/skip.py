"""Skip intros, recaps and credits – and find the next episode.

Two ways of finding the parts to skip:

* Chapters: many files name their chapters ("Intro", "Opening", "Recap", "End Credits"…).
* Audio fingerprints: episodes of a series share the same theme tune. FFmpeg's Chromaprint
  fingerprints the opening (and closing) minutes of this episode and a neighbouring one,
  and the longest stretch of identical audio is the intro (or the credits). Results are
  cached, so each episode is analysed once.
"""
import json
import os
import re
import shutil
import subprocess
import threading
from array import array
from collections import Counter

from . import DATA_DIR
from .localmedia import EXTS as VIDEO_EXT
from .health import log
from .localmedia import EPISODE  # noqa: E402

CACHE_FILE = os.path.join(DATA_DIR, "skip_cache.json")
ITEM = 0.1238          # seconds of audio per Chromaprint item (4096 / 11025 / 3)
INTRO_SCAN = 600       # look for the intro in the first 10 minutes
CREDITS_SCAN = 300     # and the credits in the last 5
MIN_LEN, MAX_INTRO = 12, 150
MAX_BITS = 6           # bits two items may differ by and still count as the same sound

CHAPTER_KINDS = (
    ("recap", re.compile(r"\b(recap|previously)\b", re.I)),
    ("intro", re.compile(r"\b(intro|opening|op|theme|title sequence|main titles?)\b", re.I)),
    ("credits", re.compile(r"\b(credits?|ending|ed|outro|end titles?)\b", re.I)),
    ("preview", re.compile(r"\b(preview|next episode|next time)\b", re.I)),
)
LABEL = {"intro": "Skip intro", "recap": "Skip recap", "credits": "Skip credits", "preview": "Skip preview"}

_lock = threading.Lock()


# ── chapters ─────────────────────────────────────────────────────────────
def from_chapters(chapters, duration):
    """[(kind, start, end)] from mpv's chapter-list."""
    out = []
    for i, ch in enumerate(chapters or []):
        title = (ch.get("title") or "").strip()
        for kind, rx in CHAPTER_KINDS:
            if rx.search(title):
                start = ch.get("time") or 0
                end = chapters[i + 1]["time"] if i + 1 < len(chapters) else duration
                if end and end - start >= 5:
                    out.append((kind, start, end))
                break
    return out


# ── episodes ─────────────────────────────────────────────────────────────
def episode_key(path):
    m = EPISODE.search(os.path.basename(path))
    if not m:
        return None
    s, e = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
    return int(s), int(e)


def siblings(path):
    """Episode files in the same folder, in order (only if `path` is an episode)."""
    if not episode_key(path):
        return []
    d = os.path.dirname(path)
    try:
        names = os.listdir(d)
    except OSError:
        return []
    eps = [os.path.join(d, n) for n in names
           if os.path.splitext(n)[1].lower() in VIDEO_EXT and episode_key(n)]
    return sorted(eps, key=lambda p: episode_key(p))


def next_episode(path):
    eps = siblings(path)
    try:
        i = eps.index(path)
    except ValueError:
        return None
    return eps[i + 1] if i + 1 < len(eps) else None


# ── audio fingerprints ───────────────────────────────────────────────────
def _ffmpeg():
    from . import fixvideo
    t = fixvideo._static() or fixvideo._system()
    return t[0] if t else shutil.which("ffmpeg")


def fingerprint(path, start=0.0, length=INTRO_SCAN):
    exe = _ffmpeg()
    if not exe:
        return None
    cmd = [exe, "-v", "error", "-nostdin"]
    if start > 0:
        cmd += ["-ss", f"{start:.1f}"]
    cmd += ["-t", str(length), "-i", path, "-vn", "-ac", "1", "-f", "chromaprint", "-fp_format", "raw", "-"]
    kw = {"creationflags": 0x08000000} if os.name == "nt" else {}
    try:
        raw = subprocess.run(cmd, capture_output=True, timeout=120, **kw).stdout
    except Exception:
        return None
    a = array("I")
    a.frombytes(raw[:len(raw) - len(raw) % 4])
    return a if len(a) > MIN_LEN / ITEM else None


def _longest_run(a, b, shift):
    """Longest stretch where a[i] ≈ b[i - shift], tolerating short glitches. -> (start_in_a, length)."""
    best = (0, 0)
    run_start, gap, last_good = None, 0, None
    lo, hi = max(0, shift), min(len(a), len(b) + shift)
    for i in range(lo, hi):
        same = (a[i] ^ b[i - shift]).bit_count() <= MAX_BITS
        if same:
            if run_start is None:
                run_start = i
            last_good, gap = i, 0
        elif run_start is not None:
            gap += 1
            if gap > 8:                       # ~1 s of different sound ends the run
                if last_good - run_start > best[1]:
                    best = (run_start, last_good - run_start + 1)
                run_start, gap = None, 0
    if run_start is not None and last_good - run_start > best[1]:
        best = (run_start, last_good - run_start + 1)
    return best


def shared_audio(a, b):
    """Longest piece of audio found in both fingerprints -> (start_a, start_b, seconds) or None."""
    index = {}
    for j, v in enumerate(b):
        index.setdefault(v, []).append(j)
    shifts = Counter()
    for i, v in enumerate(a):
        for j in index.get(v, ())[:4]:
            shifts[i - j] += 1
    best = None
    for shift, votes in shifts.most_common(8):
        if votes < 3:
            break
        start, n = _longest_run(a, b, shift)
        if n and (not best or n > best[1]):
            best = (start, n, shift)
    if not best or best[1] * ITEM < MIN_LEN:
        return None
    start, n, shift = best
    return start * ITEM, (start - shift) * ITEM, n * ITEM


# ── cache ────────────────────────────────────────────────────────────────
def _key(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return f"{path}|{st.st_size}|{int(st.st_mtime)}"


def _load():
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _store(key, segs):
    with _lock:
        c = _load()
        c[key] = segs
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(c, f)
        os.replace(tmp, CACHE_FILE)


def _duration(path):
    exe = _ffmpeg()
    probe = exe and os.path.join(os.path.dirname(exe), "ffprobe" + (".exe" if os.name == "nt" else ""))
    probe = probe if probe and os.path.isfile(probe) else shutil.which("ffprobe")
    if not probe:
        return None
    try:
        out = subprocess.run([probe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                             capture_output=True, text=True, timeout=30).stdout
        return float(out.strip())
    except Exception:
        return None


def analyse(path):
    """[(kind, start, end)] found by comparing this episode's audio with its neighbours'. Cached."""
    key = _key(path)
    if not key:
        return []
    cached = _load().get(key)
    if cached is not None:
        return [tuple(s) for s in cached]
    eps = siblings(path)
    if path not in eps or len(eps) < 2:
        return []
    i = eps.index(path)
    others = [eps[j] for j in (i + 1, i - 1, i + 2, i - 2) if 0 <= j < len(eps)]
    segs = []
    dur = _duration(path)
    scan = min(INTRO_SCAN, dur / 3) if dur else INTRO_SCAN       # the intro is in the first third
    # intro: shared audio near the start
    fa = fingerprint(path, 0, scan)
    if fa:
        for other in others[:2]:
            fb = fingerprint(other, 0, scan)
            hit = fb and shared_audio(fa, fb)
            if hit and hit[2] <= MAX_INTRO:
                segs.append(("intro", round(hit[0], 1), round(hit[0] + hit[2], 1)))
                break
    # credits: shared audio that runs to (nearly) the end
    if dur and dur > CREDITS_SCAN * 2:
        ca = fingerprint(path, dur - CREDITS_SCAN, CREDITS_SCAN)
        if ca:
            for other in others[:2]:
                od = _duration(other)
                cb = od and fingerprint(other, od - CREDITS_SCAN, CREDITS_SCAN)
                hit = cb and shared_audio(ca, cb)
                if hit:
                    start = dur - CREDITS_SCAN + hit[0]
                    if start + hit[2] > dur - 45:          # runs to the end – that's the credits
                        segs.append(("credits", round(start, 1), round(dur, 1)))
                        break
    log.info("Skip: %s → %s", os.path.basename(path), segs or "nothing found")
    _store(key, segs)
    return segs

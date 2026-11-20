"""Find every video on this computer's drives (home + mounted disks / USB)."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import urllib.parse

from . import DATA_DIR, DOWNLOAD_DIR

INDEX = os.path.join(DATA_DIR, "local.json")
THUMBS = os.path.join(DATA_DIR, "thumbs")
EXTS = {".mp4", ".m4v", ".mkv", ".webm", ".avi", ".mov", ".wmv", ".flv", ".mpg", ".mpeg",
        ".ts", ".m2ts", ".mts", ".vob", ".ogv", ".3gp", ".divx", ".rmvb", ".asf"}
MIN_SIZE = 20 * 1024 * 1024
DISK_FS = {"ext4", "ext3", "ext2", "ntfs", "ntfs3", "fuseblk", "exfat", "vfat", "btrfs", "xfs",
           "hfsplus", "f2fs", "zfs"}
SYSTEM_PREFIXES = ("/boot", "/var", "/snap", "/usr", "/opt", "/srv", "/tmp", "/etc", "/proc",
                   "/sys", "/dev", "/run/user", "/efi", "/recovery")
SKIP_DIRS = {"snap", "node_modules", "__pycache__", "site-packages", "dist-packages", "Trash",
             "steamapps", "lost+found", "$RECYCLE.BIN", "System Volume Information", "Windows",
             "Program Files", "Program Files (x86)", "ProgramData", "AppData"}

TAGS = re.compile(r"\b(2160p|1080p|720p|576p|480p|4k|uhd|hdr|blu-?ray|brrip|bdrip|dvdrip|dvd5|dvd9|dvd|"
                  r"web-?dl|webrip|web|hdtv|hdrip|amzn|nf|x264|x265|h\.?264|h\.?265|hevc|xvid|divx|"
                  r"aac|ac3|ddp?|dts|5\.1|7\.1|2ch|10bit|repack|proper|extended|remastered|"
                  r"unrated|multisubs|yify|yts|rarbg|galaxyrg|tgx|etrg)\b", re.I)
CAMERA = re.compile(r"^(dsc|img|vid|mov|mvi|gopr|gx|pxl|dji|clip|video)[_\-\s]?\d+", re.I)
EPISODE = re.compile(r"\b[Ss](\d{1,2})[ ._-]?[Ee](\d{1,3})\b|\b(\d{1,2})x(\d{2})\b")
PART = re.compile(r"\b(cd|disc|disk|part|pt)[ ._-]?(\d)\b", re.I)
GENERIC = re.compile(r"^(disc|disk|cd|part|movie|video|film|vts_\d+_\d+)[ ._-]?\d*$", re.I)


def roots():
    """Home folder plus every mounted data disk (internal partitions, USB, SD cards)."""
    out = [os.path.expanduser("~")]
    try:
        with open("/proc/mounts") as f:
            for line in f:
                dev, mnt, fs = line.split()[:3]
                mnt = mnt.replace("\\040", " ")
                if fs not in DISK_FS or mnt == "/" or mnt.startswith(SYSTEM_PREFIXES) and not \
                        mnt.startswith(("/run/media", "/media", "/mnt")):
                    continue
                if not any(mnt == r or mnt.startswith(r + "/") for r in out):
                    out.append(mnt)
    except OSError:
        pass
    for base in ("/media/" + os.environ.get("USER", ""), "/run/media/" + os.environ.get("USER", ""), "/mnt"):
        try:
            for e in os.scandir(base):
                if e.is_dir() and not any(e.path == r or e.path.startswith(r + "/") for r in out):
                    out.append(e.path)
        except OSError:
            pass
    return out


def _clean(s):
    s = re.sub(r"^\[?www\.[\w-]+\.[a-z]{2,4}\]?\s*-*\s*", "", s, flags=re.I)        # www.site.org - …
    s = re.sub(r"^\[?[\w-]+\.(org|com|net)\]?\s+-\s+", "", s, flags=re.I)             # site.com - …
    s = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "", s, flags=re.I)
    s = re.sub(r"[._]+", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip(" -[](){}")


def parse(path, size=0):
    """-> dict(title, year, kind, season, episode, part)"""
    folder = os.path.basename(os.path.dirname(path))
    stem = os.path.splitext(os.path.basename(path))[0]
    kind = "movie"
    info = {"season": None, "episode": None, "part": None, "year": None}
    if CAMERA.match(stem):
        return dict(info, title=stem, kind="home")
    m = PART.search(stem)
    if m:
        info["part"] = int(m.group(2))
    name = stem
    if GENERIC.match(stem.strip()) or len(_clean(PART.sub("", stem))) < 3:
        name = folder                   # "Disc 1.avi" → use the folder's name
    ep = EPISODE.search(name)
    if ep:
        kind = "tv"
        info["season"] = int(ep.group(1) or ep.group(3))
        info["episode"] = int(ep.group(2) or ep.group(4))
        name = name[:ep.start()]
    text = _clean(name)
    # year: last plausible 19xx/20xx that isn't the very start of the title
    tag = TAGS.search(text)
    tag_pos = tag.start() if tag else len(text)
    years = [(mm.start(), int(mm.group(1))) for mm in
             re.finditer(r"(?<![A-Za-z0-9])(19[2-9]\d|20[0-4]\d)(?![A-Za-z0-9])", text)]
    years = [(p, y) for p, y in years if p > 0 and y <= time.localtime().tm_year + 1]
    before = [py for py in years if py[0] < tag_pos]
    pick = before[-1] if before else (years[0] if years else None)
    cut = tag_pos
    if pick:
        info["year"] = pick[1]
        cut = min(cut, pick[0])
    title = _clean(PART.sub("", text[:cut])) or _clean(text) or stem
    title = re.sub(r"\s*\(\s*$", "", title)
    if title.islower() or title.isupper():           # "the_battle_of_midway" → "The Battle of Midway"
        small = {"a", "an", "and", "as", "at", "but", "by", "for", "in", "of", "on", "or", "the", "to", "vs"}
        words = title.lower().split()
        title = " ".join(w if (i and w in small) else w[:1].upper() + w[1:] for i, w in enumerate(words))
    if not info["year"] and kind != "tv":           # year often only in the folder name
        fy = re.search(r"(?<![A-Za-z0-9])(19[2-9]\d|20[0-4]\d)(?![A-Za-z0-9])", folder)
        if fy and _clean(folder).lower().startswith(title.lower()[:12]):
            info["year"] = int(fy.group(1))
    if kind == "movie" and not info["year"] and not TAGS.search(name) and size < 600 * 1024 * 1024:
        kind = "home"                   # no year / release info → personal or other video
    if info["part"] and kind != "tv":
        title += f" – Part {info['part']}"
    return dict(info, title=title, kind=kind)


def watch_dirs():
    """Folders where new videos usually appear (watched live for changes)."""
    out = []
    try:
        dl = subprocess.run(["xdg-user-dir", "DOWNLOAD"], capture_output=True, text=True, timeout=2).stdout.strip()
    except Exception:
        dl = ""
    home = os.path.expanduser("~")
    for d in (dl, os.path.join(home, "Downloads"), os.path.join(home, "Videos"), os.path.join(home, "Movies"),
              os.path.join(home, "Desktop"), DOWNLOAD_DIR):
        if d and os.path.isdir(d) and d != home and d not in out:
            out.append(d)
    out += [r for r in roots() if r != home and r not in out]
    return out


def scan(progress=None, stop=lambda: False, known=None):
    """Walk every root. Returns list of video dicts. progress(dirs, found) is called now and then.
    known: {real path: {"title", "year", "kind"}} for CineHaven's own downloads."""
    found, dirs = [], 0
    own = os.path.realpath(DOWNLOAD_DIR) + os.sep
    known = known or {}
    previous = load()
    first_seen = {v["path"]: v.get("first_seen", v.get("mtime", 0)) for v in previous}
    now = time.time()
    seen = set()
    stack = list(roots())
    while stack:
        if stop():
            break
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        dirs += 1
        with it:
            for e in it:
                name = e.name
                if name.startswith("."):
                    continue
                try:
                    if e.is_dir(follow_symlinks=False):
                        if name not in SKIP_DIRS:
                            stack.append(e.path)
                        continue
                    if not e.is_file(follow_symlinks=False):
                        continue
                    if os.path.splitext(name)[1].lower() not in EXTS:
                        continue
                    st = e.stat()
                except OSError:
                    continue
                if st.st_size < MIN_SIZE or (st.st_dev, st.st_ino) in seen:
                    continue
                if "sample" in name.lower() and st.st_size < 300 * 1024 * 1024:
                    continue
                seen.add((st.st_dev, st.st_ino))
                # sparse file = torrent download that hasn't finished
                incomplete = st.st_blocks * 512 < st.st_size * 0.9
                real = os.path.realpath(e.path)
                if real.startswith(own):
                    if incomplete:
                        continue                 # still downloading in CineHaven – shown in Library
                    v = parse(e.path, st.st_size)
                    if real in known:
                        v.update({k: val for k, val in known[real].items() if val})
                        v["source"] = "cinehaven"
                else:
                    v = parse(e.path, st.st_size)
                v.update(path=e.path, size=st.st_size, mtime=st.st_mtime, incomplete=incomplete,
                         busy=now - st.st_mtime < 20,           # still being copied / written
                         # first ever scan: use the file's own date so old videos aren't all "NEW"
                         first_seen=first_seen.get(e.path, now if previous else st.st_mtime))
                found.append(v)
        if progress and dirs % 200 == 0:
            progress(dirs, len(found))
    found.sort(key=lambda v: -v["mtime"])
    save(found)
    return found


def save(videos):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = INDEX + ".tmp"
    with open(tmp, "w") as f:
        json.dump(videos, f)
    os.replace(tmp, INDEX)


def load():
    try:
        with open(INDEX) as f:
            return [v for v in json.load(f)]
    except (OSError, ValueError):
        return []


# ── thumbnails ────────────────────────────────────────────────────────────
def thumbnail(path):
    """Path to a JPEG/PNG still for the video (GNOME's cache first, else ffmpeg)."""
    uri = "file://" + urllib.parse.quote(path)
    md5 = hashlib.md5(uri.encode()).hexdigest()
    for size in ("large",):
        p = os.path.expanduser(f"~/.cache/thumbnails/{size}/{md5}.png")
        if os.path.exists(p):
            return p
    os.makedirs(THUMBS, exist_ok=True)
    out = os.path.join(THUMBS, md5 + ".jpg")
    if os.path.exists(out) and os.path.getsize(out) > 0:
        return out
    if not shutil.which("ffmpeg"):
        return None
    for ss in ("420", "60", "3"):          # a few minutes in avoids black intro frames
        try:
            subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-ss", ss, "-i", path,
                            "-frames:v", "1", "-vf", "scale=256:-2", "-q:v", "4", out],
                           timeout=25, capture_output=True)
        except (subprocess.TimeoutExpired, OSError):
            continue
        if os.path.exists(out) and os.path.getsize(out) > 0:
            return out
    return None


def human_size(n):
    return f"{n / 1024**3:.1f} GB" if n >= 1024**3 else f"{n / 1024**2:.0f} MB"


def drive_label(path):
    home = os.path.expanduser("~")
    if path.startswith(home + "/"):
        rel = os.path.relpath(os.path.dirname(path), home)
        return "Home › " + rel.split("/")[0]
    for base in ("/media/", "/run/media/", "/mnt/"):
        if path.startswith(base):
            parts = path[len(base):].split("/")
            return parts[1] if base != "/mnt/" and len(parts) > 1 else parts[0]
    return os.path.dirname(path)

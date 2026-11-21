"""Free full-length movies on YouTube, from official rights-holder channels only.

The films come from the public RSS feeds of studio and distributor channels that
post their own films for free (ad-supported). Uploads by anyone else are never
listed. Each feed only shows a channel's latest 15 videos, so films are kept in a
local cache and the catalogue grows as the channels post more.

Films play on youtube.com in the browser, so the rights holder's ads and
regional rules apply as intended.

The user can also add their own picks by pasting a YouTube link.
"""
import html
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

from . import DATA_DIR


def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) CineHaven"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

# (channel id, display name) – official studio / distributor channels
CHANNELS = [
    ("UC8IHAQMuiJdY6ALuhG7iU8Q", "FilmRise Movies"),
    ("UCVFYikepF-avelvuIaQ_lHA", "Popcornflix"),
    ("UC9YHyj7QSkkSg2pjQ7M8Khg", "Paramount Movies"),
    ("UCGBzBkV-MinlBvHBzZawfLQ", "Movie Central"),
    ("UC2u3R3pjOiPZu4LtTlKkxdw", "Maverick Movies"),
]
FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id="
OEMBED_URL = "https://www.youtube.com/oembed?format=json&url="
WATCH_URL = "https://www.youtube.com/watch?v="

DIR = os.path.join(DATA_DIR, "youtube")
CACHE = os.path.join(DIR, "films.json")
PICKS = os.path.join(DIR, "picks.json")
REFRESH_TTL = 12 * 3600
KEEP_DAYS = 365

NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
      "m": "http://search.yahoo.com/mrss/"}
FULL_RE = re.compile(r"\bfull\b[^|]*\b(movie|film)\b", re.I)
NOT_FILM_RE = re.compile(r"#shorts|\btrailer\b|\bclip\b|\bteaser\b", re.I)
HEARTFELT_RE = re.compile(
    r"\b(drama|family|love|romance|romantic|true story|heartwarming|heartfelt|inspiring|"
    r"inspirational|father|mother|daughter|friendship|second chance|christmas)\b", re.I)
DARK_RE = re.compile(r"\b(horror|thriller|killer|murder|slasher|stalker|terror|demon|zombie|meth)\b", re.I)
ID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/|/live/)([A-Za-z0-9_-]{11})")

_lock = threading.Lock()


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save(path, data):
    os.makedirs(DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def clean_title(raw):
    """'Upside Down | Full Movie' -> 'Upside Down'.

    Movie Central style 'Hook sentence | Title | Full Thriller Movie' -> 'Title'.
    """
    parts = [p.strip() for p in re.split(r"\s+\|\s+", html.unescape(raw)) if p.strip()]
    full = next((i for i, p in enumerate(parts) if FULL_RE.search(p)), None)
    title = parts[full - 1] if full and full >= 2 else next(
        (p for p in parts if not FULL_RE.search(p)), parts[0] if parts else raw)
    return re.sub(r"\s*\((19|20)\d\d\)\s*$", "", title).strip() or raw


def _year(raw):
    m = re.search(r"\((19\d\d|20\d\d)\)", raw)
    return int(m.group(1)) if m else None


def _parse_feed(data, channel):
    out = []
    root = ET.fromstring(data)
    for e in root.findall("a:entry", NS):
        raw = e.findtext("a:title", "", NS)
        if not FULL_RE.search(raw) or NOT_FILM_RE.search(raw):
            continue
        vid = e.findtext("yt:videoId", "", NS)
        g = e.find("m:group", NS)
        desc = g.findtext("m:description", "", NS) if g is not None else ""
        out.append({
            "id": vid,
            "title": clean_title(raw),
            "raw_title": raw,
            "year": _year(raw),
            "channel": channel,
            "description": re.sub(r"\s+", " ", desc).strip()[:400],
            "published": e.findtext("a:published", "", NS),
            "thumb": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
            "seen": time.time(),
        })
    return out


def films(force=False):
    """All cached official films, newest first. Refreshes the feeds every 12 h."""
    with _lock:
        cache = _load(CACHE, {"t": 0, "films": {}})
        if force or time.time() - cache.get("t", 0) > REFRESH_TTL:
            def get(ch):
                try:
                    return _parse_feed(fetch(FEED_URL + ch[0], timeout=20), ch[1])
                except Exception:
                    return None
            with ThreadPoolExecutor(max_workers=len(CHANNELS)) as pool:
                results = list(pool.map(get, CHANNELS))
            if any(r is not None for r in results):
                for r in results:
                    for f in r or []:
                        old = cache["films"].get(f["id"], {})
                        cache["films"][f["id"]] = dict(old, **f)
                cutoff = time.time() - KEEP_DAYS * 86400
                cache["films"] = {k: v for k, v in cache["films"].items() if v.get("seen", 0) > cutoff}
                cache["t"] = time.time()
                _save(CACHE, cache)
            elif not cache["films"]:
                raise ConnectionError("Couldn't reach YouTube")
    out = list(cache["films"].values())
    out.sort(key=lambda f: f.get("published", ""), reverse=True)
    for f in out:
        f["title"] = clean_title(f["raw_title"])
        text = f["raw_title"] + " " + f["description"]
        f["heartfelt"] = bool(HEARTFELT_RE.search(text)) and not DARK_RE.search(text)
    return out


def video_id(text):
    text = (text or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", text):
        return text
    m = ID_RE.search(text)
    return m.group(1) if m else None


def picks():
    return _load(PICKS, [])


def add_pick(link):
    """Add a user's YouTube link. Returns the new pick; raises ValueError if invalid."""
    vid = video_id(link)
    if not vid:
        raise ValueError("That doesn't look like a YouTube video link.")
    url = WATCH_URL + vid
    try:
        info = json.loads(fetch(OEMBED_URL + urllib.parse.quote(url, safe=""), timeout=15))
    except Exception:
        raise ValueError("YouTube says this video is private, removed or not embeddable.")
    official = {name for _id, name in CHANNELS}
    pick = {"id": vid, "title": clean_title(info.get("title", vid)), "raw_title": info.get("title", ""),
            "year": _year(info.get("title", "")), "channel": info.get("author_name", ""),
            "description": "", "thumb": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
            "official": info.get("author_name") in official, "added": time.time()}
    with _lock:
        cur = [p for p in picks() if p["id"] != vid]
        _save(PICKS, [pick] + cur)
    return pick


def remove_pick(vid):
    with _lock:
        _save(PICKS, [p for p in picks() if p["id"] != vid])


def watch_url(vid):
    return WATCH_URL + vid


SEARCH_URL = "https://www.youtube.com/results?search_query="


def _walk(o, out):
    if isinstance(o, dict):
        if "videoRenderer" in o:
            out.append(o["videoRenderer"])
        for v in o.values():
            _walk(v, out)
    elif isinstance(o, list):
        for v in o:
            _walk(v, out)


def _minutes(length):
    try:
        parts = [int(p) for p in (length or "").split(":")]
    except ValueError:
        return 0
    while len(parts) < 3:
        parts.insert(0, 0)
    return parts[0] * 60 + parts[1] + parts[2] / 60


def search(query, limit=12):
    """Full-length films (60+ min) matching `query`, uploaded by the official channels only."""
    official = dict(CHANNELS)
    req = urllib.request.Request(SEARCH_URL + urllib.parse.quote_plus(f"{query} full movie"),
                                 headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)",
                                          "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=20) as r:
        page = r.read().decode("utf-8", "replace")
    m = re.search(r"var ytInitialData = (\{.*?\});</script>", page)
    if not m:
        return []
    found = []
    _walk(json.loads(m.group(1)), found)
    out, seen = [], set()
    for v in found:
        owner = (v.get("ownerText") or {}).get("runs", [{}])[0]
        cid = owner.get("navigationEndpoint", {}).get("browseEndpoint", {}).get("browseId")
        raw = "".join(r.get("text", "") for r in (v.get("title") or {}).get("runs", []))
        mins = _minutes((v.get("lengthText") or {}).get("simpleText"))
        if cid not in official or mins < 60 or NOT_FILM_RE.search(raw) or v["videoId"] in seen:
            continue
        seen.add(v["videoId"])
        desc = "".join(r.get("text", "") for s in v.get("detailedMetadataSnippets", [])
                       for r in s.get("snippetText", {}).get("runs", []))
        out.append({"id": v["videoId"], "title": clean_title(raw), "raw_title": raw, "year": _year(raw),
                    "channel": official[cid], "description": desc[:400], "official": True,
                    "minutes": int(mins), "thumb": f"https://i.ytimg.com/vi/{v['videoId']}/hqdefault.jpg"})
        if len(out) >= limit:
            break
    return out


# ── any YouTube video, played inside CineHaven ────────────────────────────
# yt-dlp (the open-source tool VLC and mpv use for YouTube) turns a YouTube link into the
# video and audio streams. CineHaven uses the yt-dlp installed on the system (or its Python
# module). YouTube changes often; when an old yt-dlp stops working the user is told how to
# update it, and can always open the video on youtube.com instead.
import shutil  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402

SUGGEST_URL = "https://suggestqueries.google.com/complete/search?client=firefox&ds=yt&hl=en&q="


class YouTubeError(Exception):
    pass


def _ytdlp_version(cmd):
    try:
        return subprocess.run(cmd + ["--version"], capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception:
        return ""


_cmd_cache = {}


def ytdlp_cmd():
    """(command, version) for the newest yt-dlp available: the system program or the Python module."""
    if "c" in _cmd_cache:
        return _cmd_cache["c"]
    cands = []
    exe = shutil.which("yt-dlp")
    if exe:
        cands.append([exe])
    try:
        import importlib.util
        if importlib.util.find_spec("yt_dlp"):
            cands.append([sys.executable, "-m", "yt_dlp"])
    except Exception:
        pass
    best, best_v = None, ""
    for c in cands:
        v = _ytdlp_version(c)
        if v and v > best_v:            # versions are dates (2026.10.26), so text order works
            best, best_v = c, v
    _cmd_cache["c"] = (best, best_v)
    return best, best_v


def update_hint():
    if sys.platform.startswith("win") or sys.platform == "darwin":
        return "Update CineHaven to get the newest YouTube support."
    return ("Updating yt-dlp usually fixes this: in a terminal run  pipx upgrade yt-dlp  or  "
            "sudo apt install --only-upgrade yt-dlp")


def _js_runtimes():
    """yt-dlp needs a JavaScript runtime for YouTube's player challenges; use any that's installed."""
    args = []
    for rt, name in (("deno", "deno"), ("node", "node"), ("bun", "bun"), ("qjs", "quickjs")):
        if shutil.which(rt):
            args += ["--js-runtimes", name]
    return args


def _codec_sort():
    """Prefer formats this computer's graphics chip decodes (see adapt.py)."""
    try:
        from . import adapt
        hw = (adapt._state().get("probe") or {}).get("decode") or {}
    except Exception:
        hw = {}
    if hw.get("av1"):
        return "res:1080,fps"
    if hw.get("vp9"):
        return "res:1080,fps,vcodec:vp9"
    return "res:1080,fps,vcodec:h264"


FRIENDLY = (
    (r"Sign in to confirm your age|age-restricted|inappropriate for some users",
     "YouTube only shows this video to signed-in adults"),
    (r"Private video", "this video is private"),
    (r"members-only|Join this channel", "this video is for channel members only"),
    (r"not available in your country|blocked it in your country", "this video isn't available in your country"),
    (r"Video unavailable|video is unavailable|has been removed|account associated with this video has been terminated",
     "this video was removed or is unavailable"),
    (r"confirm you.re not a bot", "YouTube is asking to confirm you're not a bot – try again in a little while"),
    (r"Premieres in|This live event will begin", "this video hasn't started yet"),
    (r"timed out|Unable to download webpage|getaddrinfo|Network is unreachable", "couldn't reach YouTube"),
)
HOPELESS = r"Private video|members-only|age|removed|terminated|country|Premieres|live event|not a bot|reach YouTube"


def _friendly(err):
    for pat, text in FRIENDLY:
        if re.search(pat, err, re.I):
            return text
    last = [ln for ln in err.splitlines() if "ERROR" in ln]
    msg = last[-1].split("ERROR:", 1)[-1].strip() if last else (err.strip().splitlines() or ["unknown problem"])[-1]
    return f"YouTube playback failed ({msg[:160]}). {update_hint()}"


def _edl(url):
    return f"%{len(url.encode())}%{url}"


def resolve(link):
    """Everything needed to play a YouTube video in mpv."""
    vid = video_id(link)
    if not vid:
        raise YouTubeError("that isn't a YouTube video link")
    cmd, _v = ytdlp_cmd()
    if not cmd:
        raise YouTubeError("YouTube playback needs yt-dlp, which isn't installed. "
                           + ("On Ubuntu: sudo apt install yt-dlp" if sys.platform.startswith("linux")
                              else update_hint()))
    args = cmd + ["-J", "--no-playlist", "--no-warnings", *_js_runtimes(),
                  "-f", "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b", "-S", _codec_sort(),
                  WATCH_URL + vid]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        raise YouTubeError("YouTube took too long to answer") from None
    if r.returncode != 0 or not r.stdout.strip():
        raise YouTubeError(_friendly(r.stderr or "no answer"))
    info = json.loads(r.stdout)
    fmts = info.get("requested_formats")
    if fmts and len(fmts) == 2:
        v = next((f for f in fmts if f.get("vcodec") not in (None, "none")), fmts[0])
        a = fmts[1] if v is fmts[0] else fmts[0]
        headers = v.get("http_headers") or info.get("http_headers") or {}
        ua = headers.get("User-Agent")
        vu = relay.add(v["url"], ua, f"video/{v.get('ext') or 'mp4'}")
        au = relay.add(a["url"], ua, f"audio/{a.get('ext') or 'mp4'}")
        play = "edl://!no_clip;!no_chapters;" + _edl(vu) + ";!new_stream;!no_clip;!no_chapters;" + _edl(au)
        quality = f"{v.get('height') or '?'}p {(v.get('vcodec') or '').split('.')[0]}"
    else:
        headers = info.get("http_headers") or {}
        play = info.get("url") or info.get("manifest_url")
        if play and info.get("protocol") == "https" and "googlevideo.com" in play:
            play = relay.add(play, headers.get("User-Agent"), f"video/{info.get('ext') or 'mp4'}")
        quality = f"{info.get('height') or '?'}p"
    if not play:
        raise YouTubeError("YouTube didn't offer a playable stream")
    subs = []
    for src in (info.get("subtitles") or {}, info.get("automatic_captions") or {}):
        for lang in ("en", "en-US", "en-GB", "en-orig"):
            vtt = next((s["url"] for s in src.get(lang, []) if s.get("ext") == "vtt"), None)
            if vtt:
                subs.append(vtt)
                break
        if subs:
            break
    return {"id": vid, "title": info.get("title") or vid,
            "channel": info.get("channel") or info.get("uploader") or "",
            "duration": info.get("duration"), "live": bool(info.get("is_live")), "url": play,
            "ua": headers.get("User-Agent"), "subs": subs, "quality": quality,
            "thumb": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"}


def start_time(link):
    """The ?t=… start point in a YouTube link, in seconds."""
    m = re.search(r"[?&#](?:t|start)=(\d+h)?(\d+m)?(\d+)s?", link or "")
    if not m:
        return None
    h = int((m.group(1) or "0h")[:-1])
    mi = int((m.group(2) or "0m")[:-1])
    return h * 3600 + mi * 60 + int(m.group(3))


def is_link(text):
    t = (text or "").strip()
    return bool(re.match(r"(https?://)?(www\.|m\.|music\.)?(youtube\.com|youtu\.be|youtube-nocookie\.com)/", t)) \
        and video_id(t) is not None


def suggest(text):
    """Search suggestions as you type."""
    try:
        data = json.loads(fetch(SUGGEST_URL + urllib.parse.quote(text), timeout=5).decode("utf-8", "replace"))
        return [s for s in data[1] if isinstance(s, str)][:8]
    except Exception:
        return []


def _text(o):
    if not o:
        return ""
    return o.get("simpleText") or "".join(r.get("text", "") for r in o.get("runs", []))


def search_all(query, limit=30):
    """Any YouTube videos matching the search, as YouTube itself ranks them."""
    req = urllib.request.Request(SEARCH_URL + urllib.parse.quote_plus(query) + "&sp=EgIQAQ%253D%253D",
                                 headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)", "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=20) as r:
        page = r.read().decode("utf-8", "replace")
    m = re.search(r"var ytInitialData = (\{.*?\});</script>", page)
    if not m:
        return []
    found = []
    _walk(json.loads(m.group(1)), found)
    official = {cid for cid, _n in CHANNELS}
    out, seen = [], set()
    for v in found:
        vid = v.get("videoId")
        if not vid or vid in seen:
            continue
        seen.add(vid)
        owner = (v.get("ownerText") or {}).get("runs", [{}])[0]
        cid = owner.get("navigationEndpoint", {}).get("browseEndpoint", {}).get("browseId")
        length = _text(v.get("lengthText"))
        live = not length and "LIVE" in json.dumps(v.get("badges", []))
        out.append({"id": vid, "title": _text(v.get("title")), "raw_title": _text(v.get("title")),
                    "channel": owner.get("text", ""), "length": length, "live": live,
                    "views": _text(v.get("shortViewCountText")), "age": _text(v.get("publishedTimeText")),
                    "year": None, "description": "", "official": cid in official, "search": True,
                    "thumb": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"})
        if len(out) >= limit:
            break
    return out


# ── relay: YouTube only serves its video files in pieces ──────────────────
# googlevideo.com answers "403 Forbidden" to an open-ended request ("give me everything from
# byte X on") and only serves bounded ranges, as yt-dlp does in 10 MB pieces. FFmpeg's reader
# always asks open-ended, so the player reads through this small relay on 127.0.0.1, which
# fetches the same bytes piece by piece and passes them straight on (seeking works as usual).
import secrets as _secrets  # noqa: E402
from http.server import BaseHTTPRequestHandler as _BH, ThreadingHTTPServer as _TS  # noqa: E402

CHUNK = 10 * 1024 * 1024


def _upstream(url, ua, start, end, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": ua or "Mozilla/5.0", "Range": f"bytes={start}-{end}"})
    return urllib.request.urlopen(req, timeout=timeout)


class _RelayHandler(_BH):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_a):
        pass

    def _target(self):
        return self.server.map.get(self.path.strip("/").split("?")[0])

    def _size(self, t):
        if t.get("size"):
            return t["size"]
        m = re.search(r"[?&]clen=(\d+)", t["url"])
        if m:
            t["size"] = int(m.group(1))
        else:
            with _upstream(t["url"], t["ua"], 0, 0) as r:
                t["size"] = int(r.headers.get("Content-Range", "/0").rsplit("/", 1)[1])
        return t["size"]

    def _head(self, t, start, end, size, partial):
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", t.get("type") or "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()

    def _range(self, size):
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if not m:
            return 0, size - 1, False
        a, b = m.groups()
        if not a:                                   # "bytes=-500": the last 500 bytes
            return max(0, size - int(b)), size - 1, True
        return int(a), min(int(b) if b else size - 1, size - 1), True

    def do_HEAD(self):
        t = self._target()
        if not t:
            return self.send_error(404)
        try:
            size = self._size(t)
        except Exception:
            return self.send_error(502)
        start, end, partial = self._range(size)
        self._head(t, start, end, size, partial)

    def do_GET(self):
        t = self._target()
        if not t:
            return self.send_error(404)
        try:
            size = self._size(t)
        except Exception:
            return self.send_error(502)
        start, end, partial = self._range(size)
        if start > end:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._head(t, start, end, size, partial)
        pos, tries = start, 0
        while pos <= end:
            stop = min(pos + CHUNK - 1, end)
            try:
                with _upstream(t["url"], t["ua"], pos, stop) as r:
                    while True:
                        block = r.read(65536)
                        if not block:
                            break
                        self.wfile.write(block)
                        pos += len(block)
                tries = 0
            except (BrokenPipeError, ConnectionResetError):
                return                               # the player moved on (seek or stop)
            except Exception:
                tries += 1                           # network hiccup: carry on from where it broke
                if tries > 4:
                    return
                time.sleep(min(2 * tries, 6))


class _Relay:
    def __init__(self):
        self.httpd = None

    def add(self, url, ua, ctype=None):
        if not self.httpd:
            self.httpd = _TS(("127.0.0.1", 0), _RelayHandler)
            self.httpd.daemon_threads = True
            self.httpd.map = {}
            threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        key = _secrets.token_urlsafe(12)
        mp = self.httpd.map
        mp[key] = {"url": url, "ua": ua, "type": ctype}
        while len(mp) > 40:                          # forget the oldest
            mp.pop(next(iter(mp)))
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/{key}"


relay = _Relay()

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

"""Free live TV channels (iptv-org public directory of free-to-air / FAST streams).

Pay-TV brands are excluded. Every stream is health-checked before it is shown,
so the list only contains channels that are on air from your connection.
"""
import json
import os
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from . import DATA_DIR

MIRRORS = ["https://raw.githubusercontent.com/iptv-org/api/gh-pages/",
           "https://cdn.jsdelivr.net/gh/iptv-org/api@gh-pages/",
           "https://iptv-org.github.io/api/"]
DIR = os.path.join(DATA_DIR, "livetv")
INDEX = os.path.join(DIR, "index.json")
HEALTH = os.path.join(DIR, "health.json")
INDEX_TTL = 3 * 86400
HEALTH_TTL = 6 * 3600

ENGLISH_COUNTRIES = {"US", "UK", "GB", "CA", "AU", "NZ", "IE", "ZA", "INT"}
GENRES = [  # (key, label, categories)
    ("series", "Series", {"series"}),
    ("movies", "Movies", {"movies"}),
    ("entertainment", "Entertainment", {"entertainment", "comedy", "classic", "family", "lifestyle"}),
    ("kids", "Kids", {"kids", "animation"}),
    ("documentary", "Documentary", {"documentary", "science", "travel", "outdoor"}),
    ("news", "News", {"news"}),
    ("music", "Music", {"music"}),
    ("sports", "Sport", {"sports"}),
]


def sections(home):
    """(key, label, filter) chips: the user's own countries first, then genres in their languages."""
    from .watch import COUNTRIES, flag
    langs = ENGLISH_COUNTRIES | set(home)
    out = [(f"country:{cc}", f"{flag(cc)} {COUNTRIES.get(cc, cc)}", lambda c, cc=cc: c["country"] == cc)
           for cc in home]
    for key, label, cats in GENRES:
        out.append((key, label, lambda c, cats=cats: bool(cats & set(c["cats"])) and c["country"] in langs))
    return out


def section_filter(key, home):
    if key.startswith("country:"):
        cc = key.split(":", 1)[1]
        return lambda c: c["country"] == cc
    return next((f for k, _l, f in sections(home) if k == key), None)


# Subscription channels – any "free" stream of these is an unofficial copy.
PAY_TV = ("osn", "star world", "star movies", "hbo", "cinemax", "showtime", "starz", "sky cinema",
          "sky atlantic", "m-net", "mnet", "dstv", "showmax", "bein", "supersport", "cartoon network",
          "nickelodeon", "amc series", "fx ", "fox movies", "sony max", "disney channel", "mbc+ ",
          "rotana cinema+", "zee cinema")
class _Flags(dict):
    def __missing__(self, cc):
        from .watch import flag
        return flag(cc)


FLAGS = _Flags()


def _get(name):
    last = None
    for m in MIRRORS:
        try:
            req = urllib.request.Request(m + name, headers={"User-Agent": "CineHaven"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except Exception as e:
            last = e
    raise last


def _is_pay(name):
    n = name.lower() + " "
    return any(n.startswith(p) or f" {p}" in f" {n}" for p in PAY_TV)


def load_index(force=False):
    """Compact list of candidate channels (rebuilt every 3 days)."""
    try:
        if not force and time.time() - os.path.getmtime(INDEX + ".v2") < INDEX_TTL:
            with open(INDEX + ".v2") as f:
                return json.load(f)
    except (OSError, ValueError):
        pass
    channels = {c["id"]: c for c in _get("channels.json")}
    streams = _get("streams.json")
    logos = {}
    for lg in _get("logos.json"):
        if lg.get("channel") and lg["channel"] not in logos and lg.get("format") in ("PNG", "JPEG", "SVG", None):
            logos[lg["channel"]] = lg["url"]
    by_ch = {}
    for s in streams:
        cid = s.get("channel")
        c = channels.get(cid)
        if not c or c.get("is_nsfw") or c.get("closed") or _is_pay(c["name"]):
            continue
        by_ch.setdefault(cid, []).append({"url": s["url"], "ua": s.get("user_agent"),
                                          "ref": s.get("referrer"), "q": s.get("quality")})
    out = []
    for cid, ss in by_ch.items():
        c = channels[cid]
        item = {"id": cid, "name": c["name"], "country": c["country"],
                "cats": c.get("categories") or [], "logo": logos.get(cid), "streams": ss[:3]}
        out.append(item)                       # every country (the user picks theirs)
    os.makedirs(DIR, exist_ok=True)
    with open(INDEX + ".v2", "w") as f:
        json.dump(out, f)
    return out


class Health:
    """Checks streams (cached for 6 h). status: True / False / None (unknown)."""

    def __init__(self):
        self.pool = ThreadPoolExecutor(max_workers=24)
        try:
            with open(HEALTH) as f:
                self.db = json.load(f)
        except (OSError, ValueError):
            self.db = {}

    def known(self, ch):
        h = self.db.get(ch["id"])
        if h and time.time() - h["t"] < HEALTH_TTL:
            return h
        return None

    def _probe(self, ch):
        for i, s in enumerate(ch["streams"]):
            h = {"User-Agent": s.get("ua") or "Mozilla/5.0 (X11; Linux x86_64)"}
            if s.get("ref"):
                h["Referer"] = s["ref"]
            try:
                with urllib.request.urlopen(urllib.request.Request(s["url"], headers=h), timeout=8) as r:
                    if r.status == 200 and r.read(1024):
                        return {"t": time.time(), "ok": True, "stream": i}
            except Exception:
                continue
        return {"t": time.time(), "ok": False}

    def check(self, ch, callback):
        """callback(ch, result) is called from a worker thread."""
        def work():
            res = self._probe(ch)
            self.db[ch["id"]] = res
            callback(ch, res)
        return self.pool.submit(work)

    def save(self):
        try:
            with open(HEALTH, "w") as f:
                json.dump(self.db, f)
        except OSError:
            pass

    def shutdown(self):
        self.save()
        self.pool.shutdown(wait=False, cancel_futures=True)

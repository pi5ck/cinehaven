"""'Where to watch' – legal streaming / rental availability per country.

Uses JustWatch's public GraphQL endpoint (the data behind justwatch.com).
Results are cached for 12 hours.
"""
import json
import os
import time
import urllib.error
import urllib.request

from . import DATA_DIR

API = "https://apis.justwatch.com/graphql"
IMG = "https://images.justwatch.com"
CACHE_FILE = os.path.join(DATA_DIR, "watch_cache.json")
CACHE_TTL = 12 * 3600

COUNTRIES = {"ZA": "South Africa", "AE": "UAE", "US": "United States", "GB": "United Kingdom",
             "CA": "Canada", "AU": "Australia", "NZ": "New Zealand", "IE": "Ireland", "IN": "India",
             "NG": "Nigeria", "KE": "Kenya", "SA": "Saudi Arabia", "EG": "Egypt", "DE": "Germany",
             "FR": "France", "NL": "Netherlands", "ES": "Spain", "IT": "Italy", "BR": "Brazil",
             "MX": "Mexico", "PH": "Philippines", "SG": "Singapore", "MY": "Malaysia"}
KIND_LABEL = {"FREE": "Free", "ADS": "Free with ads", "FLATRATE": "Subscription",
              "RENT": "Rent", "BUY": "Buy", "CINEMA": "In cinemas"}
KIND_ORDER = ["FREE", "ADS", "FLATRATE", "RENT", "BUY", "CINEMA"]
QUALITY = {"_4K": 3, "UHD": 3, "HD": 2, "SD": 1}

_FIELDS = """
  id objectType
  content(country:$c, language:"en") {
    title originalReleaseYear posterUrl shortDescription runtime
    genres { shortName }
    scoring { imdbScore }
  }
"""
_OFFERS = """
  offers(country:$c, platform:WEB) {
    monetizationType presentationType standardWebURL retailPrice(language:"en")
    package { clearName shortName }
  }
"""

_cache = None


def _load_cache():
    global _cache
    if _cache is None:
        try:
            with open(CACHE_FILE) as f:
                _cache = json.load(f)
        except (FileNotFoundError, ValueError):
            _cache = {}
    return _cache


def _save_cache():
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(_cache, f)
    os.replace(tmp, CACHE_FILE)


def _gql(query, variables):
    req = urllib.request.Request(
        API, data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"})
    for attempt in range(4):           # the API rate-limits bursts – back off and retry
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read())
            break
        except (urllib.error.URLError, TimeoutError, ValueError):
            if attempt == 3:
                raise
            time.sleep(1.5 * (attempt + 1))
    if data.get("errors"):
        raise RuntimeError(data["errors"][0].get("message", "lookup failed"))
    return data["data"]


def poster_url(path, size="s166"):
    if not path:
        return None
    return IMG + path.replace("{profile}", size).replace("{format}", "jpg")


def _title(node):
    c = node.get("content") or {}
    return {
        "id": node["id"],
        "kind": "series" if node.get("objectType") == "SHOW" else "movie",
        "title": c.get("title"),
        "year": c.get("originalReleaseYear"),
        "poster": poster_url(c.get("posterUrl")),
        "description": c.get("shortDescription") or "",
        "runtime": c.get("runtime"),
        "genres": [g["shortName"] for g in c.get("genres") or []],
        "imdb": (c.get("scoring") or {}).get("imdbScore"),
    }


def search(query, country="ZA", limit=12):
    """Movies matching `query` (title metadata only, no offers)."""
    q = ("query($q:String!,$c:Country!){popularTitles(country:$c,first:%d,"
         "filter:{searchQuery:$q,objectTypes:[MOVIE,SHOW]}){edges{node{%s}}}}" % (limit, _FIELDS))
    data = _gql(q, {"q": query, "c": country})
    return [_title(e["node"]) for e in data["popularTitles"]["edges"]]


def _norm(t):
    import re
    t = re.sub(r"[^a-z0-9 ]", " ", (t or "").lower().replace("&", "and"))
    return " ".join(w for w in t.split() if w not in ("the", "a", "an"))


def find(title, year=None, country="ZA"):
    """Best single match for a title (+ optional year). Title IDs are global, so
    if the film isn't in this country's catalogue we identify it via other ones."""
    want = _norm(title)
    for c in dict.fromkeys((country, "ZA", "AE", "US", "GB")):
        res = [r for r in search(title, c, limit=10) if r["kind"] == "movie"]
        good = [r for r in res if _norm(r["title"]) == want]
        good += [r for r in res if r not in good and _norm(r["title"]).startswith(want + " ")]
        if year:
            near = [r for r in good if r["year"] and abs(r["year"] - year) <= 1]
            good = near or ([] if any(r["year"] for r in good) else good)
        if good:
            return good[0]
    return None


def offers(title_id, country, force=False):
    """Legal options in one country, grouped & de-duplicated.

    Returns list of dicts: kind, label, service, url, price, quality – best first.
    """
    cache = _load_cache()
    key = f"{title_id}:{country}"
    hit = cache.get(key)
    if hit and not force and time.time() - hit["t"] < CACHE_TTL:
        return hit["offers"]
    q = "query($id:ID!,$c:Country!){node(id:$id){... on Movie{%s} ... on Show{%s}}}" % (_OFFERS, _OFFERS)
    node = _gql(q, {"id": title_id, "c": country}).get("node") or {}
    best = {}
    for o in node.get("offers") or []:
        kind = o["monetizationType"]
        svc = o["package"]["clearName"]
        k = (kind, svc)
        qual = QUALITY.get(o.get("presentationType"), 0)
        cur = best.get(k)
        if cur is None or qual > cur["_q"]:
            best[k] = {"kind": kind, "label": KIND_LABEL.get(kind, kind.title()), "service": svc,
                       "url": o["standardWebURL"], "price": o.get("retailPrice"),
                       "quality": o.get("presentationType"), "_q": qual}
    out = sorted(best.values(), key=lambda x: (KIND_ORDER.index(x["kind"]) if x["kind"] in KIND_ORDER else 9,
                                                x["service"]))
    for o in out:
        o.pop("_q", None)
    cache[key] = {"t": time.time(), "offers": out}
    _save_cache()
    return out


# New users start with an empty list (they add films by searching)
STARTER_LIST = []


ADULT = ("porn", "sex", "erotic", "xxx", "nude", "naked")


def documentaries(country, limit=40):
    """Popular documentaries (films + series) you can stream now with a subscription or free."""
    q = ("query($c:Country!){popularTitles(country:$c,first:%d,sortBy:POPULAR,filter:{genres:[\"doc\"],"
         "objectTypes:[MOVIE,SHOW],monetizationTypes:[FLATRATE,FREE,ADS]}){edges{node{%s %s}}}}"
         % (limit, _FIELDS, _OFFERS))
    out = []
    for e in _gql(q, {"c": country})["popularTitles"]["edges"]:
        n = e["node"]
        t = _title(n)
        if any(w in (t["title"] or "").lower() for w in ADULT):
            continue
        svc = {}
        for o in n.get("offers") or []:
            if o["monetizationType"] in ("FLATRATE", "FREE", "ADS"):
                svc.setdefault(o["package"]["clearName"], o["standardWebURL"])
        if svc:
            t["services"] = svc
            out.append(t)
    return out

"""Movie search + authenticity / rights checking.

Source: the Internet Archive's moving-image library, which publishes a
BitTorrent file (with HTTP web-seeds) for every item, and SHA-1/MD5 checksums
for every file – so downloads can be proven to be the exact original file.

Every result gets a *trust verdict*:
  VERIFIED   – known open movie, or published 1930 or earlier (US public domain)
  LIKELY_OK  – Archive lists it as public domain / Creative Commons, and the
               item sits in a curated film collection
  UNVERIFIED – rights not stated; shown only when the user asks
  BLOCKED    – modern commercial film (1978+) with no proof it is free to share:
               almost certainly an unauthorised upload, never offered
"""
import json
import re
import urllib.parse

from .engine import fetch

SEARCH_URL = "https://archive.org/advancedsearch.php"
META_URL = "https://archive.org/metadata/"
THUMB_URL = "https://archive.org/services/img/"

CURATED = {"feature_films", "silent_films", "classic_cartoons", "film_noir",
           "sci-fi_horror", "comedy_films", "prelinger", "classic_tv",
           "cartoonsandanimations", "short_films", "documentaries",
           "blender", "computersandtechvideos", "artsandmusicvideos",
           "Film_Noir", "SciFi_Horror", "moviesandfilms", "open-film-trust",
           "more_animation", "animationandcartoons", "clear-bits"}

# Distinctive titles of films their makers released openly (Blender Foundation)
OPEN_MOVIES = ("big buck bunny", "sintel", "tears of steel", "elephants dream",
               "elephant's dream", "cosmos laundromat", "caminandes",
               "sprite fright", "agent 327", "the daily dweebs")

PD_YEAR_CUTOFF = 1930        # US: works published 1930 or earlier are PD in 2026
MODERN_YEAR = 1978           # after this, copyright is automatic & unrenewed-lapse impossible

VERIFIED, LIKELY_OK, UNVERIFIED, BLOCKED = "verified", "likely", "unverified", "blocked"

VIDEO_FORMATS_PREF = ["h.264 HD", "h.264", "MPEG4", "h.264 IA", "Matroska",
                      "512Kb MPEG4", "Ogg Video", "WebM", "MPEG2", "Cinepack"]


def _first(v):
    if isinstance(v, list):
        return v[0] if v else None
    return v


def _year(doc):
    for k in ("year", "date"):
        v = _first(doc.get(k))
        if v:
            m = re.search(r"(1[89]\d\d|20\d\d)", str(v))
            if m:
                return int(m.group(1))
    return None


def _is_open_movie(title):
    t = (title or "").lower()
    return any(re.search(r"\b" + re.escape(name) + r"\b", t) for name in OPEN_MOVIES)


def _english(lang):
    """True / False if the Archive lists a language, None if it doesn't say."""
    langs = lang if isinstance(lang, list) else [lang] if lang else []
    langs = [str(l).strip().lower() for l in langs if l]
    if not langs:
        return None
    return any(l.startswith(("en", "eng")) for l in langs)


def assess(doc):
    """Return (verdict, human-readable reason)."""
    title = _first(doc.get("title")) or ""
    year = _year(doc)
    lic = (_first(doc.get("licenseurl")) or "").lower()
    cols = set(doc.get("collection") or [])
    if isinstance(doc.get("collection"), str):
        cols = {doc["collection"]}
    curated = bool(cols & CURATED)
    free_licence = "publicdomain" in lic or "creativecommons.org" in lic or "/zero/" in lic
    blender = "blender" in cols or "blender" in (_first(doc.get("creator")) or "").lower()

    if cols & {"FedFlix", "nasa"}:
        return VERIFIED, "US Government film – public domain by law"
    if "prelinger" in cols:
        return VERIFIED, "Prelinger Archives – public domain"
    if blender or _is_open_movie(title):
        return VERIFIED, "Open movie – released free by its makers"
    if year and year <= PD_YEAR_CUTOFF:
        return VERIFIED, f"Public domain – published {year}"
    if year and year >= MODERN_YEAR:
        return BLOCKED, f"{year} film – no proof it is free to share"
    if free_licence and curated:
        kind = "Public domain" if "publicdomain" in lic or "/zero/" in lic else "Creative Commons"
        return LIKELY_OK, f"{kind} per Internet Archive" + (f" · {year}" if year else "")
    if free_licence:
        return UNVERIFIED, "Licence claimed by uploader, not in a curated collection"
    return UNVERIFIED, "Rights not stated"


DOC_FILTER = ("(subject:documentary OR subject:documentaries OR collection:FedFlix OR collection:nasa"
              " OR collection:prelinger)")


def search(query, page=1, rows=40, category="movies"):
    q = query.strip()
    base = ("mediatype:movies AND -collection:movie_trailers AND -collection:movie_trailers_unsorted"
            " AND (collection:feature_films OR collection:silent_films OR collection:classic_cartoons"
            " OR collection:film_noir OR collection:sci-fi_horror OR collection:comedy_films"
            " OR collection:prelinger OR collection:blender OR collection:short_films"
            " OR collection:documentaries OR collection:moviesandfilms OR collection:open-film-trust"
            " OR collection:animationandcartoons OR collection:opensource_movies)")
    safe = re.sub(r'[\\"():^~*?\[\]{}!+-]', " ", q).strip()
    fields = ("identifier", "title", "year", "date", "downloads", "avg_rating",
              "licenseurl", "collection", "runtime", "creator", "description", "language")

    def query(expr, sort):
        params = [("q", f"({expr}) AND {base}" if expr else base), ("rows", str(rows)),
                  ("page", str(page)), ("output", "json")] + [("fl[]", f) for f in fields]
        if sort:
            params.append(("sort[]", "downloads desc"))
        return json.loads(fetch(SEARCH_URL + "?" + urllib.parse.urlencode(params))).get(
            "response", {}).get("docs", [])

    if category == "docs":
        base = ("mediatype:movies AND -collection:movie_trailers AND -collection:movie_trailers_unsorted"
                f" AND {DOC_FILTER}")
    if not safe and category == "docs":   # proper documentaries first, then gov / Prelinger films
        docs = query("(subject:documentary OR subject:documentaries) AND (collection:feature_films"
                     " OR collection:silent_films OR collection:nasa)", True)
        seen = {d["identifier"] for d in docs}
        docs += [d for d in query("collection:nasa OR collection:FedFlix", True) if d["identifier"] not in seen]
    elif not safe:   # home page: the most-watched feature films and open movies
        docs = query("collection:feature_films OR collection:open-film-trust OR collection:silent_films", True)
    else:
        # Title matches (most popular first), topped up with full-text relevance
        docs = query(f"title:({safe})", True)
        if len(docs) < 12:
            seen = {d["identifier"] for d in docs}
            docs += [d for d in query(safe, False) if d["identifier"] not in seen]
    out = []
    for d in docs:
        verdict, reason = assess(d)
        desc = _first(d.get("description")) or ""
        desc = re.sub(r"<[^>]+>", " ", str(desc))
        desc = re.sub(r"\s+", " ", desc).strip()
        out.append({
            "id": d["identifier"],
            "title": _first(d.get("title")) or d["identifier"],
            "year": _year(d),
            "downloads": d.get("downloads") or 0,
            "rating": d.get("avg_rating"),
            "runtime": _first(d.get("runtime")),
            "description": desc[:400],
            "verdict": verdict,
            "reason": reason,
            "english": _english(d.get("language")),
            "thumb": THUMB_URL + d["identifier"],
        })
    return out


def _size(f):
    try:
        return int(f.get("size") or 0)
    except ValueError:
        return 0


def resolve(identifier):
    """Fetch full metadata and pick the best playable file.

    Returns dict(torrent_url, file_name, sha1, md5, size, subtitles[], verdict, reason, title)
    """
    meta = json.loads(fetch(META_URL + urllib.parse.quote(identifier)))
    if "metadata" not in meta:
        raise RuntimeError("This title is no longer available on the Internet Archive.")
    md = meta["metadata"]
    files = meta.get("files", [])
    verdict, reason = assess(md)
    if verdict == BLOCKED:
        raise PermissionError(reason)

    torrent = next((f["name"] for f in files if f.get("format") == "Archive BitTorrent"), None)
    if not torrent:
        raise RuntimeError("The Archive has no torrent for this title yet.")

    from .safety import DANGEROUS_EXT
    cands = []
    for f in files:
        name = f["name"]
        low = name.lower()
        if any(low.endswith(e) for e in DANGEROUS_EXT):
            continue
        fmt = f.get("format", "")
        if fmt not in VIDEO_FORMATS_PREF and not low.endswith((".mp4", ".mkv", ".webm", ".ogv", ".avi", ".mpeg", ".mpg")):
            continue
        size = _size(f)
        if size <= 0:
            continue
        # Favour high-quality H.264 MP4 under ~4.5 GB (fast-start, streams best)
        score = 0
        if low.endswith(".mp4"):
            score += 40
        if "1080" in low:
            score += 20
        elif "720" in low:
            score += 15
        if "512kb" in low or "_ipod" in low or "_psp" in low or "_iphone" in low:
            score -= 25
        if fmt.startswith("h.264"):
            score += 10
        if f.get("source") == "original":
            score += 5
        if size > 4_500_000_000:
            score -= 30
        score += min(size / 200_000_000, 10)
        cands.append((score, f))
    if not cands:
        raise RuntimeError("No playable video file in this item.")
    best = max(cands, key=lambda c: c[0])[1]
    subs = [f["name"] for f in files if f["name"].lower().endswith((".srt", ".vtt"))]
    return {
        "title": _first(md.get("title")) or identifier,
        "torrent_url": f"https://archive.org/download/{urllib.parse.quote(identifier)}/{urllib.parse.quote(torrent)}",
        "file_name": best["name"],
        "sha1": best.get("sha1"),
        "md5": best.get("md5"),
        "size": _size(best),
        "length": best.get("length"),
        "subtitles": [f"https://archive.org/download/{urllib.parse.quote(identifier)}/{urllib.parse.quote(s)}" for s in subs],
        "verdict": verdict,
        "reason": reason,
    }
